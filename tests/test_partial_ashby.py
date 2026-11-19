from types import SimpleNamespace
import unittest
from unittest.mock import patch
from jobradar.providers.ashby import AshbyProvider
from jobradar.observations import PartialBoard, resolve_ashby_titles


def response(data):
    return SimpleNamespace(json=lambda:data, raise_for_status=lambda:None)


class AshbyObservationTests(unittest.TestCase):
    def partial(self):
        data={"jobs":[{"jobUrl":"https://jobs.ashbyhq.com/acme/a","title":"Engineer"},
                      {"jobUrl":"https://jobs.ashbyhq.com/acme/b","title":""},
                      {"title":"missing URL"}]}
        with patch("jobradar.providers.ashby.http_get",return_value=response(data)):
            with self.assertRaises(PartialBoard) as raised:
                AshbyProvider("acme").fetch("case")
        return raised.exception

    def test_exact_history_fills_one_field_and_does_not_certify_other_gaps(self):
        partial=self.partial()
        with patch("requests.get",return_value=response([{"title":"Last verified"}])) as get:
            jobs,retained,rejected=resolve_ashby_titles(partial,"http://example.invalid","unused")
        self.assertEqual([j.title for j in jobs],["Engineer","Last verified"])
        self.assertEqual((len(retained),rejected),(1,1))
        self.assertEqual(get.call_args.kwargs["params"]["url"],"eq.https://jobs.ashbyhq.com/acme/b")
        self.assertEqual(get.call_args.kwargs["params"]["source_payload->>provider_slug"],"eq.acme")

    def test_unknown_and_conflicting_titles_quarantined(self):
        for rows in ([],[{"title":"A"},{"title":"B"}],[{"title":"  " }]):
            with patch("requests.get", return_value=response(rows)):
                jobs, retained, unresolved=resolve_ashby_titles(self.partial(),"http://example.invalid","unused")
            self.assertEqual(([j.title for j in jobs],retained,unresolved),(["Engineer"],[],2))
