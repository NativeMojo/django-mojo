# PhoneHub Architecture

## Design Decisions

### PhoneNumber Model - Pure Cache (No User/Group Ownership)

**Decision**: The `PhoneNumber` model is intentionally NOT tied to users or groups.

**Rationale**:
1. **Minimize API Charges**: Twilio charges ~$0.015 per lookup with caller name. By sharing cached data across the entire system, we avoid redundant lookups.
2. **Maximize Cache Hits**: If User A looks up a number, and later User B looks up the same number, they both use the cached data - saving costs.
3. **Phone Data is Public**: Phone number metadata (carrier, line type, caller name) is public information from carrier databases - there's no privacy concern in sharing it.
4. **Simplicity**: One phone number = one cache record. No complex multi-tenant cache logic.

**Example Scenario**:
```
10:00 AM - Support team looks up +14155551234 (costs $0.015)
          → Cached for 30 days
          
11:00 AM - Sales team looks up same +14155551234 (costs $0.00)
          → Uses cached data
          
Next week - Marketing looks up same +14155551234 (costs $0.00)
           → Still cached
           
After 30 days - Anyone looks up +14155551234 (costs $0.015)
               → Cache expired, fresh lookup performed
```

**Result**: 3 lookups but only 1 charge instead of 3.

## Model Relationships

```
┌─────────────────┐
│   PhoneConfig   │  - Per-organization or system-wide
│                 │  - Contains API credentials (encrypted)
│ • group (FK)    │  - Configuration for SMS/lookup provider
└─────────────────┘
         │
         │ (uses for API calls)
         ▼
┌─────────────────┐
│  PhoneNumber    │  - Global cache (NO group/user FK)
│                 │  - Shared across entire system
│ • phone_number  │  - 30-day TTL (configurable)
│   (unique)      │
└─────────────────┘
         ▲
         │ (references for tracking)
         │
┌─────────────────┐
│      SMS        │  - Per-organization messages
│                 │  - Audit trail for sent/received
│ • user (FK)     │
│ • group (FK)    │
└─────────────────┘
```

## Cache Strategy

### Lookup Flow

1. **Check cache first**: Query `PhoneNumber` by phone number
2. **Check expiration**: If `lookup_expires_at` > now, use cached data
3. **Cache miss or expired**: Perform fresh lookup via Twilio
4. **Update cache**: Store/update result with new expiration

### Cache Duration

Default: 30 days (`lookup_cache_days` in PhoneConfig)

**Why 30 days?**
- Phone metadata rarely changes
- Balance between cost savings and data freshness
- Configurable per PhoneConfig

**Adjusting cache duration**:
```python
config = PhoneConfig.objects.get(id=1)
config.lookup_cache_days = 90  # 3 months
config.save()
```

## Data Flow

### Phone Lookup

```
phonehub.lookup('+14155551234')
    │
    ├─► Normalize to E.164
    │
    ├─► Check PhoneNumber cache
    │   │
    │   ├─► Found & fresh? → Return cached
    │   │
    │   └─► Not found or expired?
    │       │
    │       ├─► Get PhoneConfig (for API credentials)
    │       │
    │       ├─► Call Twilio Lookup API
    │       │   (line_type_intelligence + caller_name)
    │       │
    │       └─► Store/update PhoneNumber cache
    │
    └─► Return PhoneNumber object
```

### SMS Send

```
phonehub.send_sms('+14155551234', 'Hello')
    │
    ├─► Normalize to E.164
    │
    ├─► Get PhoneConfig (by group or system default)
    │
    ├─► Create SMS record (user, group, direction=outbound)
    │
    ├─► Call Twilio/AWS API to send
    │
    └─► Update SMS record with status/message_id
```

## Multi-Tenant Approach

### PhoneConfig: Per-Organization
- Each organization can have own Twilio/AWS credentials
- System-wide default config (group=null)
- Fallback: org config → system default

### PhoneNumber: Shared Cache
- NOT per-organization
- Shared across entire system
- Reduces costs for popular numbers

### SMS: Per-Organization
- Each SMS tied to user and/or group
- Audit trail per organization
- Billing/tracking per organization

## Cost Optimization

### Current Approach
- Single shared cache for all lookups
- 30-day default TTL
- Automatic cache checking

### Cost Example

**Without caching**:
- 1000 lookups/month × $0.015 = $15/month

**With 30-day caching (50% duplicate rate)**:
- 500 unique numbers × $0.015 = $7.50/month
- 500 cache hits × $0.00 = $0.00
- **Total: $7.50/month (50% savings)**

**With 90-day caching**:
- Even more savings if numbers repeat quarterly

### Monitoring Costs

Track lookup counts:
```python
from mojo.apps.phonehub.models import PhoneNumber

# Total API calls made
total_lookups = sum(PhoneNumber.objects.values_list('lookup_count', flat=True))

# Estimated cost
estimated_cost = total_lookups * 0.015
print(f"Estimated lookup costs: ${estimated_cost:.2f}")

# Most looked up numbers
popular = PhoneNumber.objects.order_by('-lookup_count')[:10]
for p in popular:
    print(f"{p.phone_number}: {p.lookup_count} lookups")
```

## Security

### Credentials
- Stored in `PhoneConfig.mojo_secrets` (encrypted)
- Never exposed in API responses
- Per-organization isolation

### Phone Data
- Public information (from carrier databases)
- No PII - just metadata
- Safe to share across system

### SMS Data
- Per-organization isolation via `group` FK
- User tracking via `user` FK
- Permissions enforced via RestMeta

## Future Enhancements

### Potential Improvements
1. **Redis cache**: For even faster lookups
2. **Bulk lookup API**: Batch multiple lookups
3. **Cost tracking**: Per-organization cost reporting
4. **Smart TTL**: Adjust cache duration based on lookup frequency
5. **Manual cache control**: API to refresh specific numbers

### Not Recommended
- ❌ Per-organization PhoneNumber caches (defeats cost savings)
- ❌ Per-user PhoneNumber caches (too granular, defeats purpose)
- ❌ Infinite caching (data can become stale)

## Summary

PhoneHub uses a **global shared cache** for phone lookup data to minimize API costs while maintaining per-organization configs and SMS records. This hybrid approach provides:

✅ Maximum cost savings through cache sharing  
✅ Per-organization API credentials and configurations  
✅ Per-organization SMS audit trails  
✅ Simple, maintainable architecture  
