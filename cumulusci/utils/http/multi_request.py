from itertools import chain
from concurrent.futures import as_completed
from typing import Iterable, Dict

from requests_futures.sessions import FuturesSession


class ParallelHTTP:
    """A parallelized HTTP client as a context manager"""

    def __init__(self, base_url, max_workers=32):
        self.base_url = base_url
        self.max_workers = max_workers

    def __enter__(self, *args):
        self.session = FuturesSession(max_workers=self.max_workers)
        return self

    def __exit__(self, *args):
        self.session.close()

    def _async_request(self, path, method, json=None, headers={}):
        headers = {**headers, "Accept-Encoding": "gzip"}
        return self.session.request(
            method=method,
            url=self.base_url + path.lstrip("/"),
            headers=headers,
            # data=dumps(json),
            json=json,
        )

    def do_requests(self, requests: Iterable[Dict]):
        futures = (self._async_request(**request) for request in requests)
        results = (future.result() for future in as_completed(futures))
        return results


class ParallelSalesforce(ParallelHTTP):
    """A context-managed HTTP client that can parallelize access to a Simple-Salesforce connection"""

    def __init__(self, sf, max_workers=32):
        self.sf = sf
        base_url = self.sf.base_url.rstrip("/") + "/"
        super().__init__(base_url, max_workers)

    def _async_request(self, path, method, json=None, headers={}):
        headers = {**self.sf.headers, **headers}
        return super()._async_request(path, method, json, headers)


class CompositeSalesforce:
    """Format Composte Salesforce messages"""

    def create_composite_requests(self, requests, chunk_size):
        def ensure_request_id(idx, request):
            # generate a new record with a defaulted
            return {"referenceId": f"CCI__RefId__{idx}__", **request}

        requests = [
            ensure_request_id(idx, request) for idx, request in enumerate(requests)
        ]

        return (
            {"path": "composite", "method": "POST", "json": {"compositeRequest": chunk}}
            for chunk in chunks(requests, chunk_size)
        )

    def parse_composite_results(self, composite_results):
        individual_results = chain.from_iterable(
            result.json()["compositeResponse"] for result in composite_results
        )

        return individual_results


class CompositeParallelSalesforce:
    """Salesforce Session which uses the Composite API multiple times
    in parallel.
    """

    max_workers = 32
    chunk_size = 25  # max composite batch size
    psf = None

    def __init__(self, sf, chunk_size=25, max_workers=32):
        self.sf = sf
        self.chunk_size = chunk_size
        self.max_workers = max_workers

    def open(self):
        self.psf = ParallelSalesforce(self.sf, self.max_workers)
        self.psf.__enter__()

    def close(self):
        self.psf.__exit__()

    def __enter__(self, *args):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    def do_composite_requests(self, requests):
        if not self.psf:
            raise AssertionError(
                "Session was not opened. Please call open() or use as a context manager"
            )

        comp = CompositeSalesforce()
        composite_requests = comp.create_composite_requests(requests, self.chunk_size)
        composite_results = self.psf.do_requests(composite_requests)
        individual_results = comp.parse_composite_results(composite_results)
        return list(individual_results)


# TODO: Unify with get_batch_iterator when its merged elsewhere
def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
