"""
Django management command to display model permissions.

Shows a table of all models with their corresponding RestMeta permissions.

Usage:
    # Display all model permissions
    python manage.py model_permissions

    # Show only models with specific permission types
    python manage.py model_permissions --view-only
    python manage.py model_permissions --save-only

    # Filter by app
    python manage.py model_permissions --app account

    # JSON output
    python manage.py model_permissions --json

    # Include models without RestMeta
    python manage.py model_permissions --show-all
"""

import json
from django.core.management.base import BaseCommand
from django.apps import apps
from mojo.models import MojoModel


class Command(BaseCommand):
    help = 'Display model permissions table showing RestMeta permission configurations'

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results as JSON'
        )
        parser.add_argument(
            '--app',
            type=str,
            help='Filter by app name (e.g., account, incident)'
        )
        parser.add_argument(
            '--view-only',
            action='store_true',
            help='Show only VIEW_PERMS'
        )
        parser.add_argument(
            '--save-only',
            action='store_true',
            help='Show only SAVE_PERMS'
        )
        parser.add_argument(
            '--show-all',
            action='store_true',
            help='Include models without RestMeta'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show additional details like CREATE_PERMS, DELETE_PERMS, etc.'
        )

    def handle(self, *args, **options):
        """Handle command execution."""
        json_output = options['json']
        app_filter = options['app']
        view_only = options['view_only']
        save_only = options['save_only']
        show_all = options['show_all']
        verbose = options['verbose']

        # Collect model permission data
        models_data = []

        for app_config in apps.get_app_configs():
            # Skip if filtering by app
            if app_filter and app_config.label != app_filter:
                continue

            for model in app_config.get_models():
                # Check if model has MojoModel
                if not issubclass(model, MojoModel):
                    if show_all:
                        models_data.append(self.get_model_info(model, app_config.label, has_mojo=False))
                    continue

                # Get model info
                model_info = self.get_model_info(model, app_config.label, has_mojo=True, verbose=verbose)

                # Apply filters
                if view_only and not model_info.get('view_perms'):
                    continue
                if save_only and not model_info.get('save_perms'):
                    continue

                models_data.append(model_info)

        # Sort by app and model name
        models_data.sort(key=lambda x: (x['app'], x['model']))

        # Output results
        if json_output:
            self.stdout.write(json.dumps(models_data, indent=2))
        else:
            self.display_table(models_data, verbose)

    def get_model_info(self, model, app_label, has_mojo=True, verbose=False):
        """Extract permission information from a model."""
        info = {
            'app': app_label,
            'model': model.__name__,
            'full_name': f"{app_label}.{model.__name__}",
            'has_rest_meta': hasattr(model, 'RestMeta'),
        }

        if not has_mojo or not hasattr(model, 'RestMeta'):
            info['view_perms'] = []
            info['save_perms'] = []
            if verbose:
                info['create_perms'] = []
                info['delete_perms'] = []
                info['can_delete'] = False
            return info

        # Extract RestMeta permissions
        rest_meta = model.RestMeta

        info['view_perms'] = getattr(rest_meta, 'VIEW_PERMS', [])
        info['save_perms'] = getattr(rest_meta, 'SAVE_PERMS', [])

        if verbose:
            info['create_perms'] = getattr(rest_meta, 'CREATE_PERMS', [])
            info['delete_perms'] = getattr(rest_meta, 'DELETE_PERMS', [])
            info['can_delete'] = getattr(rest_meta, 'CAN_DELETE', False)
            info['log_changes'] = getattr(rest_meta, 'LOG_CHANGES', False)
            info['owner_field'] = getattr(rest_meta, 'OWNER_FIELD', 'user')
            info['group_field'] = getattr(rest_meta, 'GROUP_FIELD', 'group')

            # Check if model has user or group fields
            field_names = [f.name for f in model._meta.get_fields()]
            info['has_user_field'] = 'user' in field_names
            info['has_group_field'] = 'group' in field_names

        return info

    def display_table(self, models_data, verbose):
        """Display results in a formatted table."""
        if not models_data:
            self.stdout.write(self.style.WARNING("No models found matching criteria."))
            return

        self.stdout.write(self.style.SUCCESS("\n=== Django-MOJO Model Permissions ===\n"))

        # Calculate column widths
        max_model_len = max(len(m['full_name']) for m in models_data)
        max_model_len = max(max_model_len, 20)

        # Header
        if verbose:
            header = f"{'Model':<{max_model_len}} | {'VIEW':<30} | {'SAVE':<30} | {'CREATE':<25} | {'DELETE':<25} | {'Del?':<5} | {'U':<2} | {'G':<2}"
            separator = "-" * (max_model_len + 155)
        else:
            header = f"{'Model':<{max_model_len}} | {'VIEW_PERMS':<40} | {'SAVE_PERMS':<40}"
            separator = "-" * (max_model_len + 88)

        self.stdout.write(header)
        self.stdout.write(separator)

        # Rows
        for model_info in models_data:
            model_name = model_info['full_name']

            if not model_info['has_rest_meta']:
                line = f"{model_name:<{max_model_len}} | {self.style.WARNING('No RestMeta')}"
                self.stdout.write(line)
                continue

            view_perms = ', '.join(model_info['view_perms']) if model_info['view_perms'] else '-'
            save_perms = ', '.join(model_info['save_perms']) if model_info['save_perms'] else '-'

            if verbose:
                create_perms = ', '.join(model_info['create_perms']) if model_info['create_perms'] else '-'
                delete_perms = ', '.join(model_info['delete_perms']) if model_info['delete_perms'] else '-'
                can_delete = '✓' if model_info['can_delete'] else '-'
                has_user = '✓' if model_info.get('has_user_field') else '-'
                has_group = '✓' if model_info.get('has_group_field') else '-'

                line = f"{model_name:<{max_model_len}} | {view_perms:<30} | {save_perms:<30} | {create_perms:<25} | {delete_perms:<25} | {can_delete:<5} | {has_user:<2} | {has_group:<2}"
            else:
                line = f"{model_name:<{max_model_len}} | {view_perms:<40} | {save_perms:<40}"

            self.stdout.write(line)

        # Summary
        self.stdout.write(separator)
        total = len(models_data)
        with_rest = sum(1 for m in models_data if m['has_rest_meta'])
        self.stdout.write(f"\nTotal models: {total}")
        self.stdout.write(f"With RestMeta: {with_rest}")
        self.stdout.write(f"Without RestMeta: {total - with_rest}\n")

        if verbose:
            self.stdout.write("\nLegend:")
            self.stdout.write("  Del? = CAN_DELETE enabled")
            self.stdout.write("  U = Has user field")
            self.stdout.write("  G = Has group field\n")
