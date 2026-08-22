from django.conf import settings
from django.db import models


class UserAddress(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="addresses"
    )
    name = models.CharField("地点名称", max_length=100)
    address = models.CharField("详细地址", max_length=255)
    city_name = models.CharField("城市", max_length=50, blank=True)
    longitude = models.DecimalField("GCJ-02经度", max_digits=10, decimal_places=7)
    latitude = models.DecimalField("GCJ-02纬度", max_digits=10, decimal_places=7)
    is_default = models.BooleanField("默认地址", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_address"
        ordering = ("-is_default", "-updated_at")
        indexes = [models.Index(fields=("user", "-updated_at"))]
        constraints = [
            models.UniqueConstraint(
                fields=("user",),
                condition=models.Q(is_default=True),
                name="uniq_default_address_per_user",
            )
        ]
        verbose_name = "用户地址"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user}: {self.name}"
