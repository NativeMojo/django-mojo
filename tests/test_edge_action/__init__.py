# Edge-deployment example coverage for the deploy-webapp GitHub Action
# (maestro #2792). Converted from a never-collected unittest.TestCase module to
# testit; `edge` bucket, opt-in serial (it mock.patches the dynamically-loaded
# example module's shared `request` attribute).
TESTIT = {
    "tier": "edge",
    "serial": True,
}
