from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.access import resolve_admin_access
from reconciliation.views import ActionInput as SafeNoteInput
from orders.models import ProviderBalancePaymentPool
from .models import FinanceRecoveryCase as Case
from .recovery import action, create_case, target
from .watchdog import health


def access_for(user, permission="finance.recovery.view"):
    access = resolve_admin_access(user)
    access.require("finance.recovery.view")
    access.require(permission)
    if not access.all_data:
        raise PermissionDenied("资金异常处置及支付巡检仅供全平台财务查看。")
    return access


def case_data(case, detail=False):
    data = {
        "id": case.pk, "case_no": str(case.public_id), "kind": case.kind,
        "kind_label": case.get_kind_display(), "target_id": case.target_id,
        "reference": case.reference, "status": case.status, "status_label": case.get_status_display(),
        "revision": case.revision, "resolution": case.resolution,
        "created_at": case.created_at, "queried_at": case.queried_at,
    }
    if detail:
        obj = target(case)
        data["current_state"] = (
            obj.channel_status_label if case.kind == "distribution" else obj.get_status_display()
            if case.kind == "withdrawal" else ("已暂停" if obj.hold_reason else "无账户暂停标记")
        )
        data["current_reason"] = (
            obj.attention_reason if case.kind == "distribution" else
            obj.hold_reason if case.kind in ("pool", "wallet") else obj.wallet.hold_reason
        ) or "仍须结合原流水、账单和处理凭证核查。"
        data["current_amount"] = (
            obj.snapshot.get("provider_amount") if case.kind == "distribution" else
            obj.amount if case.kind == "withdrawal" else
            obj.available_amount if case.kind == "wallet" else None
        )
        data["events"] = [
            {"action": event.action, "note": event.note, "evidence_reference": event.evidence_reference,
             "created_at": event.created_at, "actor": event.actor.nickname if event.actor else "系统"}
            for event in case.events.select_related("actor").order_by("pk")
        ]
    return data


class CreateInput(SafeNoteInput):
    revision = None
    action = None
    kind = serializers.ChoiceField(choices=Case.Kind)
    target_id = serializers.IntegerField(min_value=1)


class ActionInput(SafeNoteInput):
    action = serializers.ChoiceField(choices=("query", "submit", "approve", "return"))
    resolution = serializers.ChoiceField(choices=("record", "resume"), default="record")


class ListQuery(serializers.Serializer):
    view = serializers.ChoiceField(choices=("cases", "pools"), default="cases")
    page = serializers.IntegerField(min_value=1, max_value=100000, default=1)
    status = serializers.ChoiceField(choices=Case.Status, required=False)
    work_id = serializers.IntegerField(min_value=1, required=False)
    search = serializers.CharField(max_length=128, required=False, allow_blank=True)


class FinanceRecoveryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request.user)
        form = ListQuery(data=request.query_params)
        form.is_valid(raise_exception=True)
        p, qs = form.validated_data, Case.objects.all()
        for key, lookup in (("status", "status"), ("work_id", "pk"), ("search", "reference__icontains")):
            if p.get(key):
                qs = qs.filter(**{lookup: p[key]})
        qs = qs.order_by("-updated_at", "-pk")
        offset = (p["page"] - 1) * 20
        # Global pools are intentionally not exposed through the city finance page.
        pool_qs = ProviderBalancePaymentPool.objects.exclude(hold_reason="").order_by("pk")
        if p["view"] == "pools" and p.get("work_id"):
            pool_qs = pool_qs.filter(pk=p["work_id"])
        pools = [{"id": pool.pk, "reference": f"平台付款账户 #{pool.pk}", "reason": pool.hold_reason}
                 for pool in pool_qs[offset:offset + 20]]
        return Response({"data": {
            "items": [case_data(case) for case in qs[offset:offset + 20]], "total": qs.count(),
            "page": p["page"], "page_size": 20, "pools": pools, "pools_total": pool_qs.count(),
            "recovery_enabled": settings.HUIFU_FINANCE_RECOVERY_ENABLED,
        }})

    def post(self, request):
        access_for(request.user, "finance.recovery.manage")
        form = CreateInput(data=request.data)
        form.is_valid(raise_exception=True)
        p = form.validated_data
        case = create_case(kind=p["kind"], target_id=p["target_id"], actor=request.user, note=p["note"])
        return Response({"data": case_data(case, True)})


class FinanceRecoveryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        access_for(request.user)
        return Response({"data": case_data(get_object_or_404(Case, pk=pk), True)})

    def post(self, request, pk):
        form = ActionInput(data=request.data)
        form.is_valid(raise_exception=True)
        p = form.validated_data
        access_for(request.user, "finance.recovery.review" if p["action"] in ("approve", "return") else "finance.recovery.manage")
        case = get_object_or_404(Case, pk=pk)
        case = action(case.pk, actor=request.user, **p)
        return Response({"data": case_data(case, True)})


class PaymentJobHealthView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access_for(request.user)
        return Response({"data": {"items": health()}})
