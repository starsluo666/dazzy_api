import re

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.access import resolve_admin_access
from .models import BILL_TYPES, ReconciliationBatch as Batch, ReconciliationIssue as Issue, ReconciliationIssueEvent as Event


def access_for(user, permission="reconciliation.view"):
    access = resolve_admin_access(user)
    access.require("reconciliation.view")
    access.require(permission)
    if not access.all_data:
        raise PermissionDenied("渠道账单为平台级资金资料，仅全平台财务可查看。")
    return access


class Query(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, max_value=100000, default=1)
    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    bill_type = serializers.ChoiceField(choices=BILL_TYPES, required=False)
    status = serializers.ChoiceField(choices=Batch.Status, required=False)
    issue_status = serializers.ChoiceField(choices=Issue.Status, required=False)
    search = serializers.CharField(max_length=128, required=False, allow_blank=True)
    work_id = serializers.IntegerField(min_value=1, required=False)
    batch_id = serializers.UUIDField(required=False)
    view = serializers.ChoiceField(choices=("batches", "issues"), default="batches")

    def validate(self, attrs):
        if attrs.get("start") and attrs.get("end") and attrs["start"] > attrs["end"]:
            raise serializers.ValidationError("开始日期不能晚于结束日期。")
        return attrs


def batch_data(b):
    return {"id": str(b.public_id), "data_date": b.data_date, "bill_type": b.bill_type,
            "bill_label": b.get_bill_type_display(), "status": b.status, "status_label": b.get_status_display(),
            "checked_at": b.checked_at, "summary": b.summary, "error_code": b.error_code,
            "open_count": b.issues.filter(status__in=("open", "processing", "review")).count()}


def issue_data(i, detail=False):
    data = {"id": i.pk, "batch_id": str(i.batch.public_id), "data_date": i.batch.data_date,
            "bill_label": i.batch.get_bill_type_display(), "reference": i.reference,
            "category": i.category, "message": i.message, "evidence": i.evidence,
            "status": i.status, "status_label": i.get_status_display(), "revision": i.revision,
            "updated_at": i.updated_at}
    if detail:
        data["events"] = [
            {"action": e.action, "note": e.note, "evidence_reference": e.evidence_reference,
             "revision": e.revision, "created_at": e.created_at,
             "actor": (e.actor.nickname or str(e.actor.public_id)) if e.actor else "系统"}
            for e in i.events.select_related("actor").order_by("pk")
        ]
    return data


class ReconciliationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request.user)
        query = Query(data=request.query_params)
        query.is_valid(raise_exception=True)
        p = query.validated_data
        batches = Batch.objects.all()
        if "batch_id" in p:
            batches = batches.filter(public_id=p["batch_id"])
        for key, lookup in (("start", "data_date__gte"), ("end", "data_date__lte"), ("bill_type", "bill_type"), ("status", "status")):
            if key in p:
                batches = batches.filter(**{lookup: p[key]})
        if p["view"] == "issues":
            qs = Issue.objects.filter(batch__in=batches).select_related("batch")
            if "issue_status" in p:
                qs = qs.filter(status=p["issue_status"])
            if "work_id" in p:
                qs = qs.filter(pk=p["work_id"])
            elif "issue_status" not in p and request.query_params.get("include_closed") != "true":
                qs = qs.exclude(status__in=("resolved", "cleared"))
            if p.get("search"):
                qs = qs.filter(Q(reference__icontains=p["search"]) | Q(message__icontains=p["search"]))
            convert = issue_data
        else:
            qs, convert = batches, batch_data
        qs = qs.order_by("-created_at", "-pk")
        offset = (p["page"] - 1) * 20
        return Response({"data": {"items": [convert(item) for item in qs[offset:offset + 20]],
                                  "total": qs.count(), "page": p["page"], "page_size": 20,
                                  "enabled": settings.HUIFU_RECONCILIATION_ENABLED,
                                  "start_date": settings.HUIFU_RECONCILIATION_START_DATE}})


class ActionInput(serializers.Serializer):
    revision = serializers.IntegerField(min_value=1)
    action = serializers.ChoiceField(choices=("start", "submit", "approve", "return"))
    note = serializers.CharField(min_length=5, max_length=1000)
    evidence_reference = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate(self, attrs):
        # Human notes must not turn the audit log into a signed-URL/credential store.
        text = attrs["note"] + " " + attrs.get("evidence_reference", "")
        if re.search(r"https?://|-----BEGIN|(?:secret|signature|token|authorization)\s*[:=]", text, re.I):
            raise serializers.ValidationError("请填写内部凭证编号，不要粘贴下载链接、密钥或访问令牌。")
        return attrs


class ReconciliationIssueView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        access_for(request.user)
        return Response({"data": issue_data(get_object_or_404(Issue.objects.select_related("batch"), pk=pk), True)})

    def post(self, request, pk):
        form = ActionInput(data=request.data)
        form.is_valid(raise_exception=True)
        p = form.validated_data
        access_for(request.user, "reconciliation.review" if p["action"] in ("approve", "return") else "reconciliation.manage")
        # Same lock order as importer: batch -> issue. No fund/ledger model writes.
        batch_id = get_object_or_404(Issue, pk=pk).batch_id
        with transaction.atomic():
            Batch.objects.select_for_update().get(pk=batch_id)
            issue = Issue.objects.select_for_update().select_related("batch").get(pk=pk)
            if issue.revision != p["revision"]:
                raise ValidationError("记录已更新，请刷新后重新核查。")
            target = {"start": ("open", "processing"), "submit": ("processing", "review"),
                      "approve": ("review", "resolved"), "return": ("review", "processing")}
            before, after = target[p["action"]]
            if issue.status != before:
                raise ValidationError("当前状态不能执行此操作。")
            if p["action"] == "submit":
                if not p.get("evidence_reference"):
                    raise ValidationError("请填写核账凭证编号，不要填写银行卡号或账单下载链接。")
                issue.submitted_by = request.user
            if p["action"] == "approve" and issue.events.filter(actor=request.user, action__in=("start", "submit")).exists():
                raise ValidationError("核查人员不能复核自己的处理结果，请由另一位财务复核。")
            issue.status, issue.revision = after, issue.revision + 1
            issue.save()
            Event.objects.create(issue=issue, actor=request.user, action=p["action"], note=p["note"],
                                 evidence_reference=p.get("evidence_reference", ""), revision=issue.revision)
        return Response({"data": issue_data(issue, True)})
