from django.contrib import admin
from . import models


@admin.register(models.Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ['kind', 'domain', 'group', 'is_active', 'created']
    list_filter = ['kind', 'is_active', 'created']
    search_fields = ['domain', 'group__name']
    ordering = ['-created']


@admin.register(models.Inbox)
class InboxAdmin(admin.ModelAdmin):
    list_display = ['address', 'account', 'is_active', 'created']
    list_filter = ['account__kind', 'is_active', 'created']
    search_fields = ['address', 'account__domain']
    ordering = ['-created']


@admin.register(models.InboxMessage)
class InboxMessageAdmin(admin.ModelAdmin):
    list_display = ['from_address', 'to_address', 'subject', 'inbox', 'processed', 'created']
    list_filter = ['processed', 'inbox__account__kind', 'created']
    search_fields = ['from_address', 'to_address', 'subject', 'message']
    ordering = ['-created']
    readonly_fields = ['created', 'modified']


@admin.register(models.Outbox)
class OutboxAdmin(admin.ModelAdmin):
    list_display = ['address', 'account', 'group', 'is_active', 'rate_limit', 'created']
    list_filter = ['account__kind', 'is_active', 'created']
    search_fields = ['address', 'account__domain', 'group__name']
    ordering = ['-created']


@admin.register(models.OutboxMessage)
class OutboxMessageAdmin(admin.ModelAdmin):
    list_display = ['from_address', 'to_address', 'subject', 'status', 'outbox', 'created']
    list_filter = ['status', 'outbox__account__kind', 'created']
    search_fields = ['from_address', 'to_address', 'subject', 'message']
    ordering = ['-created']
    readonly_fields = ['created', 'modified', 'sent_at', 'failed_at']


# Register existing models
admin.site.register(models.Message)
admin.site.register(models.Attachment)
admin.site.register(models.Bounce)
admin.site.register(models.Complaint)
admin.site.register(models.NotifyTemplate)
