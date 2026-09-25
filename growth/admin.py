from django.contrib import admin

from .models import GrowthCampaignConfig, Invitation, NewcomerGiftGrant, NewcomerGiftItem


admin.site.register(GrowthCampaignConfig)
admin.site.register(NewcomerGiftItem)
admin.site.register(Invitation)
admin.site.register(NewcomerGiftGrant)
