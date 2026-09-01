PERMISSION_GROUPS = (
    {
        "key": "overview",
        "label": "运营总览",
        "permissions": (
            ("dashboard.view", "查看运营总览"),
        ),
    },
    {
        "key": "users",
        "label": "用户管理",
        "permissions": (
            ("user.view", "查看用户资料"),
            ("user.status.manage", "管理用户账号状态"),
            ("user.risk.manage", "管理用户风险标记"),
        ),
    },
    {
        "key": "providers",
        "label": "达人管理",
        "permissions": (
            ("provider.view", "查看达人资料"),
            ("provider.review", "审核达人入驻"),
            ("provider.manage", "管理达人接单资格"),
            ("provider.credit.adjust", "调整达人信用分"),
        ),
    },
    {
        "key": "operations",
        "label": "运营配置",
        "permissions": (
            ("service_category.view", "查看服务分类"),
            ("service_category.manage", "管理服务分类"),
            ("operations.manage", "管理平台参数与接单规则"),
        ),
    },
    {
        "key": "activities",
        "label": "活动管理",
        "permissions": (
            ("activity.view", "查看活动"),
            ("activity.review", "审核活动"),
            ("activity.manage", "管理活动状态"),
            ("activity_category.view", "查看活动分类"),
            ("activity_category.manage", "管理活动分类"),
            ("activity_report.view", "查看活动举报"),
            ("activity_report.manage", "处理活动举报"),
            ("activity_finance.view", "查看活动账务"),
            ("activity_after_sales.manage", "处理活动售后"),
            ("activity_settlement.manage", "管理活动结算"),
        ),
    },
    {
        "key": "orders",
        "label": "达人订单",
        "permissions": (
            ("order.fulfillment.view", "查看履约订单"),
            ("order.support_note.add", "添加客服跟进记录"),
            ("order.after_sales.view", "查看退款售后"),
            ("order.after_sales.review", "处理退款售后"),
        ),
    },
    {
        "key": "system",
        "label": "系统管理",
        "permissions": (
            ("organization.manage", "管理后台账号与角色"),
            ("audit.view", "查看操作审计"),
            ("system.task.view", "查看系统任务"),
            ("system.task.retry", "重试失败任务"),
        ),
    },
)


PERMISSION_CODES = frozenset(
    code
    for group in PERMISSION_GROUPS
    for code, _label in group["permissions"]
)


def permission_catalog_data():
    return [
        {
            "key": group["key"],
            "label": group["label"],
            "permissions": [
                {"code": code, "label": label}
                for code, label in group["permissions"]
            ],
        }
        for group in PERMISSION_GROUPS
    ]
