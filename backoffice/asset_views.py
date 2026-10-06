"""Curated public assets for operators; never expose customer-uploaded media here."""

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from activities.models import ActivityCategory
from mediafiles.default_avatars import is_default_avatar
from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url
from mediafiles.views import PublicImageUploadView
from providers.models import ServiceCategory

from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog


ASSET_CATEGORIES = (
    MediaAsset.Category.OPERATIONS_ICON,
    MediaAsset.Category.OPERATIONS_IMAGE,
)


class AssetQuerySerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("all", "icon", "image"), default="all")
    status = serializers.ChoiceField(choices=("active", "trash"), default="active")
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(default=1, min_value=1)
    page_size = serializers.IntegerField(default=24, min_value=1, max_value=50)


class AssetIdsSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=100,
    )


def asset_references(asset):
    # Shared system assets must not be removed through the operations library.
    if is_default_avatar(asset.object_key):
        return [{"type": "default_avatar", "id": str(asset.pk), "name": "注册默认头像（系统共用）"}]
    services = ServiceCategory.objects.filter(
        Q(icon_asset=asset) | Q(icon_object_key=asset.object_key)
    ).values("id", "name")
    activities = ActivityCategory.objects.filter(
        Q(icon_asset=asset) | Q(icon_object_key=asset.object_key)
    ).values("id", "name")
    return [
        *({"type": "service_category", "id": item["id"], "name": item["name"]} for item in services),
        *({"type": "activity_category", "id": item["id"], "name": item["name"]} for item in activities),
    ]


def asset_data(asset, *, with_references=True):
    references = asset_references(asset) if with_references else []
    return {
        "id": str(asset.pk),
        "name": asset.original_filename,
        "kind": "icon" if asset.category == MediaAsset.Category.OPERATIONS_ICON else "image",
        "object_key": asset.object_key,
        "url": build_media_url(asset.object_key),
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "status": "trash" if asset.status == MediaAsset.Status.DELETED else "active",
        "uploaded_at": asset.uploaded_at,
        "reference_count": len(references),
        "references": references,
    }


def record_asset_audit(request, access, asset, action, *, before=None, after=None):
    AdminAuditLog.objects.create(
        actor=request.user,
        organization=access.member.organization if access.member else None,
        action=action,
        target_type="operations_asset",
        target_id=str(asset.pk),
        before=before or {},
        after=after or {},
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )


class AdminAssetListUploadView(PublicImageUploadView):
    permission_classes = [IsAuthenticated]
    folder = "operations/icons"
    category = MediaAsset.Category.OPERATIONS_ICON
    field_label = "运营素材"
    max_size = 10 * 1024 * 1024

    def get(self, request):
        resolve_admin_access(request.user).require("asset.view")
        query = AssetQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = MediaAsset.objects.filter(scope=MediaAsset.Scope.PUBLIC, category__in=ASSET_CATEGORIES)
        queryset = queryset.filter(status=(
            MediaAsset.Status.DELETED if params["status"] == "trash" else MediaAsset.Status.UPLOADED
        ))
        if params["kind"] != "all":
            queryset = queryset.filter(category=(
                MediaAsset.Category.OPERATIONS_ICON if params["kind"] == "icon"
                else MediaAsset.Category.OPERATIONS_IMAGE
            ))
        if search := params.get("search", "").strip():
            queryset = queryset.filter(original_filename__icontains=search)
        total = queryset.count()
        page, size = params["page"], params["page_size"]
        assets = queryset.order_by("-created_at")[(page - 1) * size:page * size]
        return Response({"data": {
            "items": [asset_data(asset) for asset in assets],
            "pagination": {"page": page, "page_size": size, "total": total},
        }})

    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("asset.manage")
        kind = request.data.get("kind", "icon")
        if kind not in ("icon", "image"):
            raise ValidationError({"kind": "素材类型只支持图标或图片。"})
        self.category = (
            MediaAsset.Category.OPERATIONS_ICON if kind == "icon"
            else MediaAsset.Category.OPERATIONS_IMAGE
        )
        self.folder = "operations/icons" if kind == "icon" else "operations/images"
        asset = self.create_asset(request)
        record_asset_audit(
            request, access, asset, "operations_asset.upload",
            after={"kind": kind, "object_key": asset.object_key},
        )
        return Response({"data": asset_data(asset)}, status=status.HTTP_201_CREATED)


class AdminAssetBatchDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("asset.manage")
        serializer = AssetIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = list(dict.fromkeys(serializer.validated_data["ids"]))
        deleted, blocked = [], []
        with transaction.atomic():
            assets = {asset.pk: asset for asset in MediaAsset.objects.select_for_update().filter(
                pk__in=ids, scope=MediaAsset.Scope.PUBLIC,
                category__in=ASSET_CATEGORIES, status=MediaAsset.Status.UPLOADED,
            ).order_by("pk")}
            if len(assets) != len(ids):
                raise ValidationError({"ids": "包含不存在或不可删除的运营素材。"})
            for asset_id in ids:
                asset = assets[asset_id]
                references = asset_references(asset)
                if references:
                    blocked.append({"id": str(asset_id), "name": asset.original_filename, "references": references})
                    continue
                asset.status = MediaAsset.Status.DELETED
                asset.save(update_fields=("status", "updated_at"))
                record_asset_audit(
                    request, access, asset, "operations_asset.trash",
                    before={"status": "active"}, after={"status": "trash"},
                )
                deleted.append(str(asset_id))
        return Response({"data": {"deleted": deleted, "blocked": blocked}})


class AdminAssetRestoreView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        access = resolve_admin_access(request.user)
        access.require("asset.manage")
        with transaction.atomic():
            asset = MediaAsset.objects.select_for_update().filter(
                pk=asset_id, scope=MediaAsset.Scope.PUBLIC,
                category__in=ASSET_CATEGORIES, status=MediaAsset.Status.DELETED,
            ).first()
            if not asset:
                raise ValidationError({"asset_id": "回收站中没有此素材。"})
            asset.status = MediaAsset.Status.UPLOADED
            asset.save(update_fields=("status", "updated_at"))
            record_asset_audit(
                request, access, asset, "operations_asset.restore",
                before={"status": "trash"}, after={"status": "active"},
            )
        return Response({"data": asset_data(asset)})
