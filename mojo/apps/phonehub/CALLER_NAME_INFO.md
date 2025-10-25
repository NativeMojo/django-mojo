# Caller Name (CNAM) Lookup Information

## What is Caller Name Lookup?

Caller Name (CNAM) lookup retrieves the registered owner name associated with a phone number. This is the same information that appears on Caller ID displays.

## Available Data

When you lookup a phone number with PhoneHub, you can get:

- **caller_name**: The registered owner/subscriber name
- **caller_type**: BUSINESS or CONSUMER
- **Carrier information**: Mobile carrier name
- **Line type**: mobile, landline, voip, etc.

## Example

```python
from mojo.apps import phonehub

phone = phonehub.lookup('+14155551234')

if phone.caller_name:
    print(f"Registered to: {phone.caller_name}")
    print(f"Type: {phone.caller_type}")  # BUSINESS or CONSUMER
else:
    print("Caller name not available")

print(f"Carrier: {phone.carrier}")
print(f"Line Type: {phone.line_type}")
```

## Use Cases

1. **Identity Verification**: Verify that a phone number is registered to the expected person/business
2. **Fraud Prevention**: Check if caller name matches claimed identity
3. **Business Validation**: Verify business phone numbers
4. **User Onboarding**: Pre-fill registration forms with CNAM data
5. **Call Screening**: Display registered name before answering

## Important Notes

### Twilio Charges

⚠️ **Caller Name lookup incurs additional charges from Twilio**

- Standard Twilio Lookup API: ~$0.005 per lookup
- **Caller Name add-on**: ~$0.01 additional per lookup
- Total cost: ~$0.015 per full lookup with caller name

Check current Twilio pricing: https://www.twilio.com/lookup/pricing

### Availability

- **Not all numbers have CNAM data available**
- Landlines typically have more complete CNAM records
- Mobile numbers may have limited or no CNAM data
- VOIP numbers often lack CNAM information
- International numbers have varying CNAM availability

### Privacy Considerations

- CNAM data is public information from carrier databases
- Data accuracy depends on carrier records
- Names may be outdated if phone number was transferred
- Business numbers typically have more accurate data

## Configuration

PhoneHub automatically requests caller name data with every lookup via Twilio. The data is cached for 30 days (configurable).

### To minimize costs:

```python
from mojo.apps import phonehub

# Check cache first (no additional lookup)
try:
    phone = phonehub.PhoneNumber.objects.get(phone_number='+14155551234')
    if not phone.needs_lookup:
        # Use cached data - no charge
        print(f"Cached: {phone.caller_name}")
    else:
        # Refresh lookup - will incur charges
        phone = phonehub.lookup(phone.phone_number, force_refresh=True)
except phonehub.PhoneNumber.DoesNotExist:
    # First lookup - will incur charges
    phone = phonehub.lookup('+14155551234')
```

### Adjust cache duration:

```python
from mojo.apps.phonehub.models import PhoneConfig

config = PhoneConfig.objects.get(id=1)
config.lookup_cache_days = 90  # Cache for 90 days instead of 30
config.save()
```

## Error Handling

```python
from mojo.apps import phonehub

phone = phonehub.lookup('+14155551234')

if phone:
    if phone.caller_name:
        print(f"✓ Caller name: {phone.caller_name}")
    else:
        print("✗ Caller name not available for this number")
        print(f"  Carrier: {phone.carrier}")
        print(f"  Type: {phone.line_type}")
else:
    print("✗ Lookup failed - invalid number or API error")
```

## Twilio Error Codes

Common Twilio error codes for caller name:

- **63206**: Caller name not available (not an error - just unavailable)
- **63210**: Caller name blocked or restricted
- **63211**: Caller name service temporarily unavailable

PhoneHub handles these gracefully and stores what data is available.

## Best Practices

1. **Cache lookups**: Use the 30-day cache to avoid repeated charges
2. **Check before lookup**: Query database first to see if data exists
3. **Handle missing data**: Not all numbers will have caller name
4. **Batch lookups carefully**: Each lookup costs money
5. **Monitor costs**: Track lookup_count field to estimate costs

## Data Accuracy

CNAM data accuracy varies:

- **High accuracy**: Business landlines, toll-free numbers
- **Medium accuracy**: Personal landlines
- **Low accuracy**: Mobile numbers, VOIP numbers
- **No data**: Prepaid phones, recent number transfers, international

Always have a fallback when caller name is unavailable.

## Alternative: Lookup Without Caller Name

If you don't need caller name data and want to save costs, you can:

1. **Modify the service** to skip caller_name field in Twilio request
2. **Use AWS SNS** (no caller name, but free basic info)
3. **Use third-party services** for free basic carrier lookup

Current implementation always requests caller name for completeness. To disable:

```python
# In services/phone.py, change:
lookup = client.lookups.v2.phone_numbers(phone_number).fetch(
    fields='line_type_intelligence'  # Remove 'caller_name'
)
```

## Summary

- ✅ Caller name provides valuable identity verification
- ⚠️ Additional charges apply (~$0.01 per lookup)
- 📦 Data is cached for 30 days by default
- 🔍 Not available for all phone numbers
- 💡 Check cache before performing new lookups
