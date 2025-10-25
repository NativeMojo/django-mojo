"""
Optional Django admin configuration for PhoneHub models.
Register these in your Django project's admin.py if needed.
"""

from django.contrib import admin
from .models import PhoneNumber, PhoneConfig, SMS


@admin.register(PhoneNumber)
class PhoneNumberAdmin(admin.ModelAdmin):
    list_display = ['phone_number', 'country_code', 'carrier', 'line_type',
                   'is_mobile', 'is_voip', 'is_valid', 'lookup_expires_at']
    list_filter = ['is_mobile', 'is_voip', 'is_valid', 'line_type', 'country_code']
    search_fields = ['phone_number', 'carrier']
    readonly_fields = ['created', 'modified', 'last_lookup_at']
    date_hierarchy = 'created'


@admin.register(PhoneConfig)
class PhoneConfigAdmin(admin.ModelAdmin):
    list_display = ['name', 'group', 'provider', 'is_active', 'test_mode',
                   'lookup_enabled', 'created']
    list_filter = ['provider', 'is_active', 'test_mode', 'lookup_enabled']
    search_fields = ['name']
    readonly_fields = ['created', 'modified', 'mojo_secrets']
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'group', 'is_active', 'test_mode')
        }),
        ('Provider Settings', {
            'fields': ('provider', 'twilio_from_number', 'aws_region', 'aws_sender_id')
        }),
        ('Lookup Settings', {
            'fields': ('lookup_enabled', 'lookup_cache_days')
        }),
        ('System Fields', {
            'fields': ('created', 'modified', 'mojo_secrets'),
            'classes': ('collapse',)
        })
    )


@admin.register(SMS)
class SMSAdmin(admin.ModelAdmin):
    list_display = ['direction', 'from_number', 'to_number', 'status',
                   'provider', 'is_test', 'created']
    list_filter = ['direction', 'status', 'provider', 'is_test']
    search_fields = ['from_number', 'to_number', 'body', 'provider_message_id']
    readonly_fields = ['created', 'modified', 'sent_at', 'delivered_at']
    date_hierarchy = 'created'
    fieldsets = (
        ('Message Info', {
            'fields': ('direction', 'from_number', 'to_number', 'body')
        }),
        ('Status', {
            'fields': ('status', 'provider', 'provider_message_id', 'is_test')
        }),
        ('Error Info', {
            'fields': ('error_code', 'error_message'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('metadata', 'user', 'group'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created', 'modified', 'sent_at', 'delivered_at'),
            'classes': ('collapse',)
        })
    )
