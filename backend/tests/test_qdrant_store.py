import unittest
from unittest.mock import MagicMock, patch

from services.qdrant_store import count_local_tags


class LocalTagCountTests(unittest.TestCase):
    def test_search_is_part_of_count_filter(self):
        client = MagicMock()
        client.count.return_value.count = 7

        with patch("services.qdrant_store.get_qdrant_client", return_value=client):
            count = count_local_tags(country="DE", category="apparel", search="coat")

        self.assertEqual(count, 7)
        count_filter = client.count.call_args.kwargs["count_filter"]
        dumped = count_filter.model_dump(exclude_none=True)
        conditions = {condition["key"]: condition["match"] for condition in dumped["must"]}
        self.assertEqual(conditions["country"]["value"], "DE")
        self.assertEqual(conditions["category"]["value"], "apparel")
        self.assertEqual(conditions["word"]["text"], "coat")


if __name__ == "__main__":
    unittest.main()
