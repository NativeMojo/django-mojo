from mojo.apps.logit.models import Log
import json
from django.utils.safestring import mark_safe

from django.contrib import admin

# from django.contrib.sites.models import Site

# Unregister Group and Site from admin
# admin.site.unregister(Group)
# admin.site.unregister(Site)

@admin.register(Log)
class LogAdmin(admin.ModelAdmin):
    exclude = ("log",)
    list_display = ("created", "kind", "method", "path", "ip", "uid", "model_name", "model_id", "log_summary")
    list_filter = ("kind", "ip", "uid", "model_name", "model_id", "created")
    search_fields = ("log", "path", "ip", "model_name")
    readonly_fields = ("created", "kind", "path", "ip", "uid", "model_name", "model_id", "user_agent", "method")
    date_hierarchy = "created"
    ordering = ("-created",)

    def formatted_log(self, obj):
        """Attempt to format the log as JSON if possible, otherwise return raw log."""
        if obj.log:
            try:
                json_data = json.loads(obj.log)  # Try parsing as JSON
                formatted_json = json.dumps(json_data, indent=4, sort_keys=True)
                return mark_safe(f"<pre>{formatted_json}</pre>")  # Wrap in <pre> for better formatting
            except json.JSONDecodeError:
                return obj.log  # Return raw log if not JSON
        return None

    def log_summary(self, obj):
        """Show a shortened version of the log for better readability."""
        return obj.log[:75] + "..." if obj.log and len(obj.log) > 75 else obj.log
    log_summary.short_description = "Log Preview"
