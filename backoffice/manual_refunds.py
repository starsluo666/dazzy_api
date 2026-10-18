"""Manual post-settlement after-sales. No gateway, wallet mutation or refund tasks."""

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.models import ProviderOrder, ProviderOrderDistribution, ProviderOrderSettlement
from .access import client_ip, resolve_admin_access
from .models import AdminAuditLog, ProviderManualRefundCase as Case
from .refund_authorization import has

OPEN = ("pending", "processing", "reviewing")
NOTICE = "人工登记不发起退款、不修改渠道状态。财务核实资金处理结果并复核后结案；原支付、分账及收入流水保持不变。"


def assert_no_manual_refund(order):
    # Completed manual refunds also block automatic money operations permanently.
    # Reopening/re-splitting requires a separately verified reconciliation workflow.
    if order.manual_refund_cases.exclude(status="rejected").exists():
        raise ValidationError("本单已有人工售后资金记录，请由财务核查，不能重复自动退款或分账。")


def assert_distribution_verified(order):
    # Caller holds the order lock in an atomic action. Serialize with query-result
    # updates so a newly recorded evidence conflict cannot race this approval.
    distribution = ProviderOrderDistribution.objects.select_for_update().filter(settlement__order=order).first()
    if distribution and (distribution.status not in ("succeeded", "failed") or distribution.evidence_conflict_code or distribution.attention_reason):
        raise ValidationError("渠道分账结果仍待核实，请先核实原分账流水，再登记或核验人工退款凭证。")


def context(order):
    cases = order.manual_refund_cases.all()
    completed = cases.filter(status="completed").aggregate(n=Sum("requested_amount"))["n"] or 0
    occupied = cases.filter(status__in=OPEN).aggregate(n=Sum("requested_amount"))["n"] or 0
    open_case = cases.filter(status__in=OPEN).first()
    automatic = order.refund_orders.aggregate(n=Sum("refund_amount"))["n"] or 0
    eligible = ProviderOrderDistribution.objects.filter(settlement__order=order).exists() or ProviderOrderSettlement.objects.filter(order=order, status="settled").exists()
    remaining = max(0, order.payable_amount - automatic - completed - occupied)
    reason = ""
    payment = getattr(order, "payment_order", None)
    if not order.paid_at or not eligible:
        reason = "仅已进入渠道分账或已结算订单可转人工售后，其余订单请使用原售后流程。"
    elif not payment or payment.status not in ("paid", "partially_refunded") or payment.payable_amount != order.payable_amount:
        reason = "原支付记录或金额尚未核实，请先核账。"
    elif open_case:
        reason = "已有未结束的人工售后，请处理原工单。"
    elif order.refund_orders.exclude(status="succeeded").exists():
        reason = "存在待核实的自动退款，请先核实原退款流水，不能同时人工处理。"
    elif order.after_sales_cases.filter(status__in=("pending", "processing", "approved")).exists():
        reason = "已有未结束的原售后申请，请先核实处理，不能重复登记。"
    elif not remaining:
        reason = "本订单已无可申请金额。"
    return {"eligible": eligible, "remaining": remaining, "completed_amount": completed,
            "occupied_amount": occupied, "open_case_no": open_case.case_no if open_case else "",
            "blocked_reason": reason, "notice": NOTICE}


def scoped(access):
    access.require("order.after_sales.view")
    qs = Case.objects.select_related("order__provider", "order__customer", "created_by", "processed_by", "reviewed_by")
    return qs if access.all_data else qs.filter(order__provider__service_city_code__in=access.city_codes)


def actions(case, access, actor):
    finance = has(access, "order.finance.view") and has(access, "order.finance.manage")
    supervisor = finance and has(access, "refund.supervise")
    result = []
    if finance and case.status == "pending":
        result.append("start")
    if finance and case.status == "processing":
        result.append("submit_evidence")
    if supervisor and case.status in ("pending", "processing") and not case.receipt_no:
        result.append("reject")
    if supervisor and case.status == "reviewing" and actor.pk not in (case.created_by_id, case.processed_by_id):
        # A handover after a returned case must not let an earlier evidence
        # submitter review their own payment evidence under a new processor ID.
        submitted_evidence = AdminAuditLog.objects.filter(
            target_type="manual_refund", target_id=case.case_no,
            action="manual_refund.submit_evidence", actor=actor,
        ).exists()
        if not submitted_evidence:
            result.extend(("verify", "return"))
    return result


def payload(case, access, actor, *, detail=False):
    data = {"case_no": case.case_no, "order_no": case.order.order_no,
            "customer_name": case.order.customer.nickname, "provider_name": case.order.provider.display_name,
            "requested_amount": case.requested_amount, "reason": case.reason,
            "status": case.status, "status_label": case.get_status_display(), "revision": case.revision,
            "created_at": case.created_at, "updated_at": case.updated_at,
            "created_by": case.created_by.nickname,
            "processed_by": case.processed_by.nickname if case.processed_by_id else "",
            "reviewed_by": case.reviewed_by.nickname if case.reviewed_by_id else "",
            "receipt_no": case.receipt_no or "", "evidence_reference": case.evidence_reference,
            "handling_note": case.handling_note, "review_note": case.review_note,
            "paid_at": case.paid_at, "closed_at": case.closed_at,
            "actions": actions(case, access, actor), "notice": NOTICE}
    if detail:
        data["records"] = list(AdminAuditLog.objects.filter(target_type="manual_refund", target_id=case.case_no)
                               .order_by("id").values("action", "actor__nickname", "after", "created_at"))
    return data


def audit(request, access, case, action, before):
    AdminAuditLog.objects.create(actor=request.user, organization=access.member.organization if access.member else None,
        action=f"manual_refund.{action}", target_type="manual_refund", target_id=case.case_no,
        before=before, after={"status": case.status, "revision": case.revision,
            "amount": case.requested_amount, "order_no": case.order.order_no, "reason": case.reason,
            "receipt_no": case.receipt_no or "", "evidence_reference": case.evidence_reference,
            "handling_note": case.handling_note, "review_note": case.review_note,
            "paid_at": case.paid_at.isoformat() if case.paid_at else None},
        request_id=request.headers.get("X-Request-ID", ""), ip_address=client_ip(request))


class CreateInput(serializers.Serializer):
    order_no = serializers.CharField(max_length=64)
    request_key = serializers.UUIDField()
    requested_amount = serializers.IntegerField(min_value=1, max_value=2147483647)
    reason = serializers.CharField(min_length=5, max_length=1000)


class ListInput(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, max_value=100000, default=1)
    search = serializers.CharField(max_length=100, allow_blank=True, default="")
    status = serializers.ChoiceField(choices=["", *Case.Status.values], default="")


class ManualRefundListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .operations_queue import filter_work_queue
        access = resolve_admin_access(request.user)
        form = ListInput(data=request.query_params)
        form.is_valid(raise_exception=True)
        params = form.validated_data
        qs = filter_work_queue(scoped(access), access, request.query_params, ("manual_refund_processing", "manual_refund_review"))
        if params["status"]:
            qs = qs.filter(status=params["status"])
        if params["search"]:
            qs = qs.filter(Q(case_no__icontains=params["search"]) | Q(order__order_no__icontains=params["search"]))
        offset = (params["page"] - 1) * 20
        return Response({"data": {"items": [payload(c, access, request.user) for c in qs[offset:offset + 20]],
                                  "total": qs.count(), "page": params["page"], "page_size": 20}})

    @transaction.atomic
    def post(self, request):
        from .refund_views import refund_source
        access = resolve_admin_access(request.user)
        access.require("order.after_sales.create")
        form = CreateInput(data=request.data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        source = refund_source(access, "provider", values["order_no"])
        order = ProviderOrder.objects.select_for_update().get(pk=source.pk)
        existing = Case.objects.filter(request_key=values["request_key"]).first()
        if existing:
            if (existing.order_id, existing.requested_amount, existing.reason, existing.created_by_id) != (order.pk, values["requested_amount"], values["reason"], request.user.pk):
                raise ValidationError("请求标识已使用，不能修改原申请。")
            return Response({"data": payload(existing, access, request.user)})
        state = context(order)
        if state["blocked_reason"]:
            raise ValidationError(state["blocked_reason"])
        if values["requested_amount"] > state["remaining"]:
            raise ValidationError("申请金额超过剩余可申请金额。")
        try:
            with transaction.atomic():
                case = Case.objects.create(order=order, created_by=request.user,
                    request_key=values["request_key"], requested_amount=values["requested_amount"], reason=values["reason"])
        except IntegrityError:
            raise ValidationError("申请已存在或发生并发冲突，请刷新核对原记录。") from None
        audit(request, access, case, "create", {})
        return Response({"data": payload(case, access, request.user)}, status=201)


class ActionInput(serializers.Serializer):
    action = serializers.ChoiceField(choices=("start", "submit_evidence", "verify", "return", "reject"))
    revision = serializers.IntegerField(min_value=1)
    note = serializers.CharField(min_length=5, max_length=1000)
    receipt_no = serializers.CharField(max_length=128, required=False)
    evidence_reference = serializers.CharField(min_length=5, max_length=500, required=False)
    paid_at = serializers.DateTimeField(required=False)


class ManualRefundDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, case_no):
        access = resolve_admin_access(request.user)
        case = get_object_or_404(scoped(access), case_no=case_no)
        return Response({"data": payload(case, access, request.user, detail=True)})

    @transaction.atomic
    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        form = ActionInput(data=request.data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        found = get_object_or_404(scoped(access), case_no=case_no)
        # Same order lock as refunds/distribution: manual handling cannot race dispatch.
        ProviderOrder.objects.select_for_update().get(pk=found.order_id)
        case = Case.objects.select_for_update().get(pk=found.pk)
        action = values["action"]
        access.require("order.finance.view")
        access.require("order.finance.manage")
        if action in ("verify", "return", "reject"):
            access.require("refund.supervise")
        if values["revision"] != case.revision:
            raise ValidationError("工单已更新，请刷新后重新核对。")
        if action not in actions(case, access, request.user):
            raise ValidationError("当前状态不可执行此操作；申请人和曾登记凭证的人员均不能复核本单。")
        before = {"status": case.status, "revision": case.revision}
        now = timezone.now()
        if action == "start":
            case.status, case.processed_by, case.handling_note = "processing", request.user, values["note"]
        elif action == "submit_evidence":
            if not all(values.get(k) for k in ("receipt_no", "evidence_reference", "paid_at")):
                raise ValidationError("请填写实际退款流水号、凭证存放位置及实际退款时间。")
            if not case.order.paid_at <= values["paid_at"] <= now:
                raise ValidationError("实际退款时间不能早于支付时间或晚于当前时间。")
            if case.receipt_no and case.receipt_no != values["receipt_no"]:
                raise ValidationError("原退款流水号已锁定，补充凭证不能替换流水号，请核对原记录。")
            assert_distribution_verified(case.order)
            case.receipt_no, case.evidence_reference, case.paid_at = values["receipt_no"], values["evidence_reference"], values["paid_at"]
            case.status, case.processed_by, case.handling_note = "reviewing", request.user, values["note"]
        elif action == "return":
            case.status, case.review_note = "processing", values["note"]
        elif action == "verify":
            assert_distribution_verified(case.order)
            case.status, case.reviewed_by, case.review_note, case.closed_at = "completed", request.user, values["note"], now
        else:
            case.status, case.reviewed_by, case.review_note, case.closed_at = "rejected", request.user, values["note"], now
        case.revision += 1
        try:
            with transaction.atomic():
                case.save()
        except IntegrityError:
            raise ValidationError("退款流水号已关联其他人工售后，请核对，不能重复登记。") from None
        audit(request, access, case, action, before)
        return Response({"data": payload(case, access, request.user, detail=True)})
