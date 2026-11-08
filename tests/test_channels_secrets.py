import unittest
from orchd.channels import secret_store


class SecretStoreTest(unittest.TestCase):
    def test_missing_provider_fails_closed(self):
        with self.assertRaises(secret_store.SecretUnavailable):
            secret_store.resolve("os-store:demo/email/read", adapter="email", source="demo", provider=None)

    def test_fake_provider_only_allows_its_provisioned_source(self):
        fake = secret_store.FakeSecretStore({("email", "demo", "os-store:demo/email/read"): b"synthetic"})
        self.assertEqual(secret_store.resolve("os-store:demo/email/read", adapter="email", source="demo", provider=fake), b"synthetic")
        with self.assertRaises(secret_store.SecretUnavailable):
            secret_store.resolve("os-store:demo/email/read", adapter="slack", source="demo", provider=fake)

    def test_invalid_reference_cannot_reach_provider(self):
        class Provider:
            def get(self, *args):
                raise AssertionError("provider should not be called")
        for ref in ("raw-token", "os-store:../escape", "os-store:/absolute", "os-store:a//b"):
            with self.subTest(ref=ref), self.assertRaises(secret_store.SecretUnavailable):
                secret_store.resolve(ref, adapter="email", source="demo", provider=Provider())
