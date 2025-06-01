from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from .models import User, Group, GroupMember


class UserAdmin(admin.ModelAdmin):
    """Custom Admin for the User model"""

    # Fields displayed in the User list in Django Admin
    list_display = ('username', 'email', 'first_name', 'last_name', 'is_staff', 'is_active', 'last_login', 'id')
    list_filter = ('is_staff', 'is_active')

    # Fields displayed when viewing/editing a user
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('Personal Info'), {'fields': ('first_name', 'last_name', 'email', 'phone_number', 'display_name')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser')}),
        (_('Important Dates'), {'fields': ('last_login', 'date_joined', 'last_activity')}),
        (_('Metadata'), {'fields': ('permissions', 'metadata')}),
    )

    # Fields displayed when adding a new user
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'password'),
        }),
    )

    # Searchable fields in the admin panel
    search_fields = ('username', 'email', 'first_name', 'last_name')
    ordering = ('username',)

    # Read-only fields for security
    readonly_fields = ('last_login', 'date_joined', 'last_activity')

admin.site.register(User, UserAdmin)



class GroupAdmin(admin.ModelAdmin):
    """Custom Admin for the Group model"""

    # Fields displayed in the Group list in Django Admin
    list_display = ('name', 'uuid', 'kind', 'is_active', 'created', 'modified', 'parent', 'id')
    list_filter = ('is_active', 'kind')

    # Searchable fields
    search_fields = ('name', 'uuid', 'kind')

    # Read-only fields for security
    readonly_fields = ('created', 'modified')

    # Fields displayed when viewing/editing a group
    fieldsets = (
        (None, {'fields': ('name', 'uuid', 'kind', 'is_active', 'parent')}),
        ('Metadata', {'fields': ('metadata',)}),
        ('Timestamps', {'fields': ('created', 'modified')}),
    )

    autocomplete_fields = ('parent',)


admin.site.register(Group, GroupAdmin)



class GroupMemberAdmin(admin.ModelAdmin):
    """Custom Admin for the GroupMember model"""

    # Fields displayed in the GroupMember list in Django Admin
    list_display = ('user', 'group', 'is_active', 'created', 'modified', 'id')
    list_filter = ('is_active', 'group')

    # Searchable fields
    search_fields = ('user__username', 'user__email', 'group__name')

    # Read-only fields for security
    readonly_fields = ('created', 'modified')

    # Fields displayed when viewing/editing a group member
    fieldsets = (
        (None, {'fields': ('user', 'group', 'is_active')}),
        ('Permissions', {'fields': ('permissions',)}),
        ('Metadata', {'fields': ('metadata',)}),
        ('Timestamps', {'fields': ('created', 'modified')}),
    )

    autocomplete_fields = ('group', 'user', )

admin.site.register(GroupMember, GroupMemberAdmin)
