from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from providers.models import ProviderTrainingConfig, ProviderTrainingVersion
from providers.training import CurriculumSerializer
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog


class TrainingEditConflict(APIException):
    status_code = 409
    default_detail = "配置已被其他管理员修改，请刷新后重新编辑。"


class RevisionSerializer(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)


class DraftSerializer(RevisionSerializer):
    draft = CurriculumSerializer()


def config_data(config):
    version = config.active_version if config else None
    return {
        "revision": config.revision if config else 0,
        "draft": config.draft if config and config.draft else {
            "title": "首次接单学习", "pass_score": 100, "lessons": [], "questions": [],
        },
        "published": {"version_id": version.pk, "title": version.payload["title"],
                      "published_at": version.created_at, "lesson_count": len(version.payload["lessons"]),
                      "question_count": len(version.payload["questions"]),
                      "pass_score": version.payload["pass_score"]} if version else None,
    }


def audit(request, access, config, action):
    AdminAuditLog.objects.create(
        actor=request.user, organization=access.member.organization if access.member else None,
        action=action, target_type="provider_training", target_id="1", ip_address=client_ip(request),
        after={"revision": config.revision, "active_version_id": config.active_version_id},
    )


class AdminProviderTrainingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        resolve_admin_access(request.user).require("operations.manage")
        config = ProviderTrainingConfig.objects.select_related("active_version").filter(pk=1).first()
        return Response({"data": config_data(config)})

    @transaction.atomic
    def patch(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        serializer = DraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config, _ = ProviderTrainingConfig.objects.select_for_update().get_or_create(pk=1)
        if config.revision != serializer.validated_data["revision"]:
            raise TrainingEditConflict()
        # Serializer representation converts UUIDs to JSON-safe strings.
        config.draft = serializer.data["draft"]
        config.revision += 1
        config.save(update_fields=("draft", "revision", "updated_at"))
        audit(request, access, config, "provider_training.save_draft")
        return Response({"data": config_data(config)})


class AdminProviderTrainingPublishView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        serializer = RevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = ProviderTrainingConfig.objects.select_for_update().filter(pk=1).first()
        if not config or config.revision != serializer.validated_data["revision"]:
            raise TrainingEditConflict()
        curriculum = CurriculumSerializer(data=config.draft)
        curriculum.is_valid(raise_exception=True)
        if not curriculum.validated_data["lessons"] or not curriculum.validated_data["questions"]:
            raise ValidationError("发布前至少添加一篇学习资料和一道题目。")
        if config.active_version_id and config.active_version.payload == curriculum.data:
            return Response({"data": config_data(config)})
        version = ProviderTrainingVersion.objects.create(payload=curriculum.data, created_by=request.user)
        config.active_version = version
        config.revision += 1
        config.save(update_fields=("active_version", "revision", "updated_at"))
        audit(request, access, config, "provider_training.publish")
        return Response({"data": config_data(config)})
