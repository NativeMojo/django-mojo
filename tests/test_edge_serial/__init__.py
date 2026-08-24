TESTIT = {
    # The serial sibling of test_edge (maestro #2792). These deploy-plane
    # tests reload the shared test server via th.server_settings, mutate
    # global hosting settings (EDGE_RELEASE_BUCKETS, EDGE_WEBAPP_CNAME_TARGET,
    # EDGE_NODE_ID via _helpers.with_setting), or patch shared model methods —
    # each unsafe beside other threads. They run in the framework preset (they
    # are django-mojo's own deploy contracts, not opt-in) but serially, out of
    # the parallel ring. The `extended` files/tests here are the heavy
    # feature-internal matrices, opt-in via their own tier tags.
    "default_core": True,
    "serial": True,
    "cold_budget": 216,
}
