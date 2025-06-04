from objict import objict
from django.db import models as dm
import string
from rest.encryption import ENCRYPTER, DECRYPTER
from datetime import datetime, date

class MetaDataBase(dm.Model):
    class Meta:
        abstract = True

    category = dm.CharField(db_index=True, max_length=32, default=None, null=True, blank=True)
    key = dm.CharField(db_index=True, max_length=80)
    value_format = dm.CharField(max_length=16)
    value = dm.TextField()
    int_value = dm.IntegerField(default=None, null=True, blank=True)
    float_value = dm.FloatField(default=None, null=True, blank=True)

    def set_value(self, value):
        self.value = str(value)
        value_type = type(value)

        if value_type is int or self.value in ["0", "1"]:
            if value_type is int and value > 2147483647:
                self.value_format = "S"
                return
            self.value_format = "I"
            self.int_value = value
        elif value_type is float:
            self.value_format = "F"
            self.float_value = value
        elif isinstance(value, list):
            self.value_format = "L"
        elif isinstance(value, dict):
            self.value_format = "O"
        elif isinstance(value, str) and len(value) < 9 and value.isdigit():
            self.value_format = "I"
            self.int_value = int(value)
        elif value in ["True", "true", "False", "false"]:
            self.value_format = "I"
            self.int_value = 1 if value.lower() == "true" else 0
        elif isinstance(value, bool):
            self.value_format = "I"
            self.int_value = 1 if value else 0
        else:
            self.value_format = "S"

    def get_strict_type(self, field_type):
        try:
            return field_type(self.value)
        except (ValueError, TypeError):
            if field_type is bool:
                return self.int_value != 0 if self.value_format == 'I' else self.value.lower() in ['true', '1', 'y', 'yes']
            elif field_type in [date, datetime]:
                return rh.parseDate(self.value)
            return self.value

    def get_value(self, field_type=None):
        if field_type:
            return self.get_strict_type(field_type)
        if self.value_format == 'I':
            return self.int_value
        elif self.value_format == 'F':
            return self.float_value
        elif self.value_format in ["L", "O"] and self.value:
            try:
                return eval(self.value)
            except Exception:
                pass
        return self.value

    def __str__(self):
        return f"{self.category}.{self.key}={self.value}" if self.category else f"{self.key}={self.value}"

class MetaDataModel:
    def set_metadata(self, request, values=None):
        if not self.id:
            self.save()

        values = values or request
        if isinstance(values, list):
            values = objict({k: v for item in values if isinstance(item, dict) for k, v in item.items()})

        if not isinstance(values, dict):
            raise ValueError(f"invalid metadata: {values}")

        for key, value in values.items():
            cat, key = key.split('.', 1) if '.' in key else (None, key)
            self.set_property(key, value, cat, request=request)

    def metadata(self):
        return self.get_properties()

    def remove_properties(self, category=None):
        self.properties.filter(category=category).delete()

    def get_properties(self, category=None):
        result = {}
        for prop in self.properties.all():
            category_ = prop.category
            key = prop.key

            if not category_:
                self._add_property_to_result(result, prop)
                continue

            props = self.get_field_props(category_)
            if props.hidden:
                continue

            if category_ not in result:
                result[category_] = {}

            if category_ == "secrets":
                masked_value = "*" * prop.int_value if prop.int_value else "******"
                result[category_][key] = masked_value
            else:
                self._add_property_to_result(result[category_], prop)

        return result.get(category, {}) if category else result

    def _add_property_to_result(self, result_dict, prop):
        props = self.get_field_props(prop.key)
        if not props.hidden:
            result_dict[prop.key] = prop.get_value()

    def get_field_props(self, key):
        self._init_field_props()
        category, key = key.split('.', 1) if '.' in key else (None, key)
        props = objict()

        if self.__field_props:
            cat_props = self.__field_props.get(category, {})
            self._update_props_with_category(props, cat_props)

            field_props = self.__field_props.get(key, {})
            self._update_props_with_field(props, field_props)

        return props

    def _update_props_with_category(self, props, cat_props):
        if cat_props:
            props.notify = cat_props.get("notify")
            props.requires = cat_props.get("requires")
            props.hidden = cat_props.get("hidden", False)
            on_change_name = cat_props.get("on_change")
            if on_change_name:
                props.on_change = getattr(self, on_change_name, None)

    def _update_props_with_field(self, props, field_props):
        props.notify = field_props.get("notify", props.notify)
        props.requires = field_props.get("requires", props.requires)
        props.hidden = field_props.get("hidden", props.hidden)
        on_change_name = field_props.get("on_change")
        if on_change_name:
            props.on_change = getattr(self, on_change_name, None)

    def check_field_perms(self, full_key, props, request=None):
        if not props.requires:
            return True
        if not request or not request.member:
            return False
        if request.member.hasPermission(props.requires) or request.user.is_superuser:
            return True

        if props.notify and request.member:
            subject = f"permission denied changing protected '{full_key}' field"
            msg = f"permission denied changing protected field '{full_key}'\nby user: {request.user.username}\nfor: {self}"
            request.member.notifyWithPermission(props.notify, subject, msg, email_only=True)
        raise re.PermissionDeniedException(subject, 481)

    def set_properties(self, data, category=None, request=None, using=None):
        for k, v in data.items():
            self.set_property(k, v, category, request=request, using=using)

    def set_property(self, key, value, category=None, request=None, using=None, ascii_only=False, encrypted=False):
        if ascii_only and isinstance(value, str):
            value = ''.join(filter(lambda x: x in string.printable, value))

        if using is None:
            using = getattr(self.RestMeta, "DATABASE", None)

        if request is None:
            request = rh.getActiveRequest()

        self._init_field_props()

        if '.' in key:
            category, key = key.split('.', 1)

        full_key = f"{category}.{key}" if category else key
        field_props = self.get_field_props(full_key)

        if not self.check_field_perms(full_key, field_props, request):
            return False

        prop = self.properties.filter(category=category, key=key).last()
        if not prop and (value is None or value == ""):
            return False

        has_changed, old_value = self._update_or_create_property(prop, category, key, value, encrypted, using)

        if has_changed and field_props.on_change:
            field_props.on_change(key, value, old_value, category)

        self._notify_change_if_required(field_props, full_key, value, request)

        if hasattr(self, "_recordRestChange"):
            self._recordRestChange(f"metadata.{full_key}", old_value)

        return has_changed

    def _update_or_create_property(self, prop, category, key, value, encrypted, using):
        has_changed = False
        old_value = None

        value_len = len(value) if encrypted else 0
        if encrypted:
            value = ENCRYPTER.encrypt(value)

        if prop:
            old_value = prop.get_value()
            if value is None or value == "":
                self.properties.filter(category=category, key=key).delete()
                has_changed = True
            else:
                has_changed = str(value) != prop.value
                if has_changed:
                    prop.set_value(value)
                    if encrypted:
                        prop.int_value = value_len
                    prop.save(using=using)
        else:
            has_changed = True
            PropClass = self.get_fk_model("properties")
            prop = PropClass(parent=self, key=key, category=category)
            prop.set_value(value)
            prop.save(using=using)

        return has_changed, old_value

    def _notify_change_if_required(self, field_props, full_key, value, request):
        if field_props.notify and request and request.member:
            username = request.member.username if request and request.member else "root"
            truncated_value = "***" if value and len(str(value)) > 5 else value
            msg = (f"protected field '{full_key}' changed to '{truncated_value}'\n"
                   f"by user: {username}\nfor: {self}")
            request.member.notifyWithPermission(field_props.notify, f"protected '{full_key}' field changed", msg, email_only=True)

    def get_property(self, key, default=None, category=None, field_type=None, decrypted=False):
        category, key = key.split('.', 1) if '.' in key else (category, key)

        try:
            prop_value = self.properties.get(category=category, key=key).get_value(field_type)
            return DECRYPTER.decrypt(prop_value) if decrypted and prop_value else prop_value
        except Exception:
            return default

    def set_secret_property(self, key, value):
        return self.set_property(key, value, category="secrets", encrypted=True)

    def get_secret_property(self, key, default=None):
        return self.get_property(key, default, "secrets", decrypted=True)
