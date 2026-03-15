# from testit import helpers as th

# TEST_USER = "testit"
# TEST_PWORD = "testit##mojo"

# ADMIN_USER = "tadmin"
# ADMIN_PWORD = "testit##mojo"

# TEST_DOMAIN = "testit.example.com"



# @th.django_unit_setup()
# def setup_users(opts):
#     from mojo.apps.account.models import User
#     from mojo.apps.aws.models import EmailDomain

#     user = User.objects.filter(username=TEST_USER).last()
#     if user is None:
#         user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
#         user.save()
#     user.save_password(TEST_PWORD)
#     user.remove_all_permissions()

#     user = User.objects.filter(username=ADMIN_USER).last()
#     if user is None:
#         user = User(username=ADMIN_USER, display_name=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
#         user.save()
#     user.remove_permission(["manage_groups"])
#     user.add_permission(["manage_users", "view_global", "view_admin"])
#     user.is_staff = True
#     user.is_superuser = True
#     user.save_password(ADMIN_PWORD)

#     # Create a test EmailDomain directly via ORM — bypasses on_rest_created so
#     # no SES/SNS calls are made. get_or_create keeps setup idempotent.
#     domain, _ = EmailDomain.objects.get_or_create(
#         name=TEST_DOMAIN,
#         defaults={"status": "pending", "region": "us-east-1"},
#     )
#     opts.domain_id = domain.pk
#     opts.domain_name = domain.name



# @th.unit_test("user_jwt_login")
# def test_user_jwt_login(opts):
#     resp = opts.client.login(TEST_USER, TEST_PWORD)
#     assert opts.client.is_authenticated, "authentication failed"
#     assert opts.client.jwt_data.uid is not None, "missing user id"
#     resp = opts.client.get(f"/api/user/{opts.client.jwt_data.uid}")
#     assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
#     assert resp.response.data.id == opts.client.jwt_data.uid
#     assert resp.response.data.username == TEST_USER, f"username: {resp.response.data.username }"
#     opts.user_id = opts.client.jwt_data.uid

# @th.unit_test("admin_jwt_login")
# def test_admin_jwt_login(opts):
#     resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
#     assert opts.client.is_authenticated, "authentication failed"
#     assert opts.client.jwt_data.uid is not None, "missing user id"
#     resp = opts.client.get(f"/api/user/{opts.client.jwt_data.uid}")
#     assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
#     assert resp.response.data.id == opts.client.jwt_data.uid, f"invalid user id {resp.response.data.id}"
#     assert resp.response.data.username == ADMIN_USER, f"username: {resp.response.data.username }"
#     opts.admin_id = opts.client.jwt_data.uid


# @th.unit_test("aws_test_email_domain")
# def test_aws_test_email_domain(opts):
#     resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
#     assert opts.client.is_authenticated, "authentication failed"
#     resp = opts.client.get(f"/api/aws/email/domain/{opts.domain_id}")
#     assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
#     assert resp.response.data is not None, "missing response data for domain"
#     assert resp.response.data.name == TEST_DOMAIN, \
#         f"Expected domain name {TEST_DOMAIN}, got {resp.response.data.name}"

# @th.unit_test("aws_email_domain_audit")
# def test_aws_email_domain_audit(opts):
#     # Ensure admin
#     resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
#     assert opts.client.is_authenticated, "authentication failed"
#     # First audit pass (no changes should be made by this endpoint)
#     resp1 = opts.client.post(f"/api/aws/email/domain/{opts.domain_id}/audit", json={})
#     assert resp1.status_code == 200, f"audit status {resp1.status_code}"
#     assert resp1.response.data is not None, "missing audit data"
#     # The audit 'data' includes: domain, region, status, items[], checks{}
#     assert resp1.response.data.domain == opts.domain_name, f"mismatched domain in audit: {resp1.response.data.domain}"
#     assert resp1.response.data.status in ["ok", "drifted", "conflict"], f"unexpected audit status {resp1.response.data.status}"
#     items = resp1.response.data.get("items")
#     assert isinstance(items, list), f"audit items should be a list, {str(items)}"
#     assert hasattr(resp1.response.data, "checks"), "missing checks in audit response"
#     # minimally assert that SES identity is verified for a known domain
#     assert resp1.response.data.checks.get("ses_verified") is True, "SES identity not verified"
#     # Second audit pass to check idempotency (still read-only, should not change status just by auditing)
#     resp2 = opts.client.post(f"/api/aws/email/domain/{opts.domain_id}/audit", json={})
#     assert resp2.status_code == 200, f"audit status {resp2.status_code}"
#     assert resp2.response.data.domain == opts.domain_name, "domain name changed between audits"
#     items = resp1.response.data.get("items")
#     assert isinstance(items, list), "audit items should be a list (2nd pass)"
#     assert hasattr(resp2.response.data, "checks"), "missing checks in audit response (2nd pass)"
#     assert hasattr(resp2.response.data, "audit_pass"), "missing audit_pass in audit response (2nd pass)"



# @th.unit_test("aws_email_domain_verify_topics_persisted")
# def test_aws_email_domain_verify_topics_persisted(opts):
#     # Fetch the domain again and ensure topic ARN fields exist on the model (read-only validation)
#     resp = opts.client.get(f"/api/aws/email/domain/{opts.domain_id}")
#     assert resp.status_code == 200, f"Expected status_code is 200 but got {resp.status_code}"
#     data = resp.response.data
#     topic_fields = [
#         "sns_topic_bounce_arn",
#         "sns_topic_complaint_arn",
#         "sns_topic_delivery_arn",
#         "sns_topic_inbound_arn",
#     ]
#     # Fields should exist; values may be None if not configured yet
#     for f in topic_fields:
#         assert hasattr(data, f), f"missing field on EmailDomain: {f}"
