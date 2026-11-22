"""Global-only moderation desk; decisions never change financial lifecycles."""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.nicknames import matching_text
from backoffice.access import client_ip, resolve_admin_access
from backoffice.models import AdminAuditLog
from mediafiles.models import MediaAsset
from . import services, targets
from .gateway import media_url, DetectionUnavailable
from .models import ContentKeywordRule, ContentReview as Review


def access_for(user, *, write=False):
    access = resolve_admin_access(user)
    access.require("content.review.view")
    if write:
        access.require("content.review.manage")
    if not access.all_data:
        raise PermissionDenied("内容审核中心仅供全平台内容运营使用。")
    return access


def review_data(review, *, detail=False):
    data = {
        "id": review.pk,
        "public_id": str(review.public_id),
        "kind": review.kind,
        "kind_label": review.get_kind_display(),
        "target_id": review.target_id,
        "owner": str(review.owner.public_id),
        "status": review.status,
        "status_label": review.get_status_display(),
        "revision": review.revision,
        "is_current": review.is_current,
        "reason": review.reason,
        "machine_suggestion": review.machine_suggestion,
        "created_at": review.created_at,
        "title": next(iter(review.snapshot.get("text", {}).values()), "图片或视频内容")[:80],
    }
    if detail:
        data["local_findings"] = review.local_findings
        try:
            obj = targets.target(review.kind, review.target_id)
        except serializers.ValidationError:
            obj = None
        data["target_available"] = obj is not None
        _, published = targets.published_target(review.kind, obj) if obj else (None, None)
        hold = getattr(published, "content_hidden_review", None)
        blocking = (
            Review.objects.filter(public_id=hold).exclude(pk=review.pk).first() if hold else None
        )
        data["blocking_review"] = (
            {"id": blocking.pk, "public_id": str(blocking.public_id)} if blocking else None
        )
        data["text"] = review.snapshot.get("text", {})
        data["assets"] = []
        for pinned in review.snapshot.get("assets", []):
            asset = MediaAsset.objects.filter(pk=pinned["id"]).first()
            url = None
            try:
                if asset and targets._media([asset])[0] == pinned:
                    url = media_url(asset)
            except (DetectionUnavailable, serializers.ValidationError):
                pass
            data["assets"].append({"id": pinned["id"], "kind": pinned["kind"], "url": url})
        data["items"] = list(
            review.items.values(
                "position",
                "kind",
                "state",
                "suggestion",
                "label",
                "score",
                "error_code",
                "request_id",
                "task_id",
            )
        )
        data["events"] = [
            {
                "action": event.action,
                "revision": event.revision,
                "actor": str(event.actor.public_id) if event.actor_id else "系统",
                "note": event.note,
                "created_at": event.created_at,
            }
            for event in review.events.select_related("actor").order_by("-pk")[:100]
        ]
    return data


class Filters(serializers.Serializer):
    todo = serializers.ChoiceField(choices=("content_review",), required=False)
    work_id = serializers.CharField(max_length=18, required=False)
    page = serializers.IntegerField(default=1, min_value=1)
    page_size = serializers.IntegerField(default=20, min_value=1, max_value=100)
    status = serializers.ChoiceField(choices=Review.Status.choices, required=False)
    kind = serializers.ChoiceField(choices=Review.Kind.choices, required=False)
    q = serializers.CharField(max_length=100, required=False, allow_blank=True)


class Register(serializers.Serializer):
    def validate_kind(self, value):
        if value == "nickname":
            raise serializers.ValidationError("昵称使用敏感词自动校验，无需登记人工审核。")
        return value

    kind = serializers.ChoiceField(choices=Review.Kind.choices)
    target_id = serializers.IntegerField(min_value=1)


class Decision(serializers.Serializer):
    action = serializers.ChoiceField(choices=("approve", "reject", "withdraw", "restore", "retry"))
    revision = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(min_length=5, max_length=500)


class ContentReviewListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request.user)
        query = Filters(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        rows = Review.objects.select_related("owner").filter(is_current=True)
        from backoffice.operations_queue import filter_work_queue

        rows = filter_work_queue(rows, access_for(request.user), params, {"content_review"})
        for key in ("kind", "status"):
            if key in params:
                rows = rows.filter(**{key: params[key]})
        if params.get("q"):
            from orders.models import ProviderOrderReview

            order_reviews = ProviderOrderReview.objects.filter(order__order_no__icontains=params["q"])
            rows = rows.filter(
                Q(public_id__icontains=params["q"]) | Q(search_text__icontains=params["q"])
                | Q(kind=Review.Kind.REVIEW, target_id__in=order_reviews.values("pk"))
            )
        total = rows.count()
        start = (params["page"] - 1) * params["page_size"]
        return Response(
            {
                "data": {
                    "items": [
                        review_data(row) for row in rows[start : start + params["page_size"]]
                    ],
                    "pagination": {
                        "page": params["page"],
                        "page_size": params["page_size"],
                        "total": total,
                    },
                    "enabled": settings.CONTENT_REVIEW_ENABLED,
                    "automatic": settings.TENCENT_CONTENT_REVIEW_ENABLED and services.gateway.has_credentials(),
                }
            }
        )

    def post(self, request):
        access_for(request.user, write=True)
        form = Register(data=request.data)
        form.is_valid(raise_exception=True)
        kind, pk = form.validated_data["kind"], form.validated_data["target_id"]
        review = services.submit(kind, targets.target(kind, pk), force=True)
        return Response({"data": review_data(review, detail=True)}, status=201)


class ContentReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        access_for(request.user)
        return Response({"data": review_data(get_object_or_404(Review, pk=pk), detail=True)})

    def post(self, request, pk):
        access_for(request.user, write=True)
        form = Decision(data=request.data)
        form.is_valid(raise_exception=True)
        get_object_or_404(Review, pk=pk)
        review = services.decide(pk, actor=request.user, **form.validated_data)
        return Response({"data": review_data(review, detail=True)})


class KeywordRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContentKeywordRule
        fields = ("id", "keyword", "scope", "label", "enabled", "updated_at")

    def validate(self, attrs):
        if "keyword" in attrs:
            normalized = matching_text(attrs["keyword"])
            if not normalized or len(normalized) > 120:
                raise serializers.ValidationError({"keyword": "请输入有效的文字或数字关键词。"})
            attrs["normalized_keyword"] = normalized
        return attrs


def save_keyword_rule(request, access, *, pk=None):
    try:
        with transaction.atomic():
            instance = None if pk is None else get_object_or_404(ContentKeywordRule.objects.select_for_update(), pk=pk)
            before = KeywordRuleSerializer(instance).data if instance else {}
            form = KeywordRuleSerializer(instance, data=request.data, partial=instance is not None)
            form.is_valid(raise_exception=True)
            rule = form.save()
            data = KeywordRuleSerializer(rule).data
            AdminAuditLog.objects.create(
                actor=request.user, organization=access.member.organization if access.member else None,
                action="content.keyword_rule.create" if instance is None else "content.keyword_rule.update",
                target_type="content_keyword_rule", target_id=str(rule.pk), before=before, after=data,
                request_id=request.headers.get("X-Request-ID", "")[:64], ip_address=client_ip(request),
            )
            return data
    except IntegrityError as exc:
        raise serializers.ValidationError({"keyword": "相同范围已有此关键词，请编辑原规则。"}) from exc


class ContentKeywordRuleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request.user)
        class Query(serializers.Serializer):
            page = serializers.IntegerField(default=1, min_value=1)
            q = serializers.CharField(required=False, allow_blank=True, max_length=60)

        query = Query(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = query.validated_data["page"]
        rows = ContentKeywordRule.objects.all()
        if search := query.validated_data.get("q"):
            rows = rows.filter(Q(keyword__icontains=search) | Q(label__icontains=search))
        offset = (page - 1) * 20
        return Response({"data": {
            "items": KeywordRuleSerializer(rows[offset:offset + 20], many=True).data,
            "pagination": {"page": page, "page_size": 20, "total": rows.count()},
        }})

    def post(self, request):
        access = access_for(request.user, write=True)
        return Response({"data": save_keyword_rule(request, access)}, status=201)


class ContentKeywordRuleDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        access = access_for(request.user, write=True)
        return Response({"data": save_keyword_rule(request, access, pk=pk)})
