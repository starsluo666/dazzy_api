from dataclasses import dataclass

from rest_framework.exceptions import PermissionDenied

from .models import AdminRole, OrganizationMember


@dataclass(frozen=True)
class AdminAccess:
    member: OrganizationMember | None
    permissions: frozenset[str]
    all_data: bool
    city_codes: frozenset[str]

    def require(self, permission: str) -> None:
        if "*" not in self.permissions and permission not in self.permissions:
            raise PermissionDenied("当前后台账号无此操作权限。")


def resolve_admin_access(user) -> AdminAccess:
    if user.is_superuser:
        return AdminAccess(None, frozenset({"*"}), True, frozenset())
    membership = (
        OrganizationMember.objects.select_related("organization", "role")
        .filter(
            user=user,
            is_active=True,
            organization__status="active",
        )
        .order_by("id")
        .first()
    )
    if not membership:
        raise PermissionDenied("当前账号未开通运营后台权限。")
    role = membership.role
    all_data = role.data_scope == AdminRole.DataScope.ALL
    cities = set(membership.organization.city_codes) | set(membership.city_codes)
    return AdminAccess(membership, frozenset(role.permissions), all_data, frozenset(cities))


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
