"""Browser page reuse must not change HTTP scheduling or durable task scopes."""

import pytest
from fixture_adapter import FixtureAdapter

from social_crawler.adapters.samples import Samples
from social_crawler.domain.models import RunConfig
from social_crawler.orchestration.worker import run_worker


@pytest.mark.parametrize('adapter', ['browser', 'httpx'])
async def test_worker_finishes_current_note_before_switching_browser_page(store, tmp_path, adapter):
    seen = []

    class RecordingAdapter(FixtureAdapter):
        async def fetch(self, request):
            seen.append((str(request.operation), request.content_id))
            return await super().fetch(request)

    config = RunConfig(
        platform='xhs', keywords=['one'], adapter=adapter, min_interval=0,
        content_limit=2, comment_limit=2, reply_parents=1, reply_limit=2,
    )
    rid = store.create_run(config, mode='offline')
    await run_worker(store, rid, lambda budget: RecordingAdapter('xhs', budget, Samples(tmp_path)))
    assert store.get_run(rid)['status'] == 'completed'
    note_steps = [(op, cid) for op, cid in seen if op != 'search']
    if adapter == 'browser':
        assert note_steps == [
            ('detail', 'synthetic-1'), ('comments', 'synthetic-1'), ('replies', 'synthetic-1'),
            ('detail', 'synthetic-2'), ('comments', 'synthetic-2'), ('replies', 'synthetic-2'),
        ]
    else:
        assert note_steps[:2] == [('detail', 'synthetic-1'), ('detail', 'synthetic-2')]
    assert len(store.snapshot(rid)['tasks']) == 7


async def test_browser_finishes_a_keywords_notes_before_the_next_search(store, tmp_path):
    seen = []

    class RecordingAdapter(FixtureAdapter):
        async def fetch(self, request):
            seen.append((str(request.operation), request.keyword or request.content_id))
            return await super().fetch(request)

    config = RunConfig(
        platform='xhs', keywords=['one', 'two'], adapter='browser', min_interval=0,
        content_limit=1, comment_limit=0, reply_parents=0, reply_limit=0,
    )
    rid = store.create_run(config, mode='offline')
    await run_worker(store, rid, lambda budget: RecordingAdapter('xhs', budget, Samples(tmp_path)))
    assert store.get_run(rid)['status'] == 'completed'
    operations = [op for op, _ in seen]
    # Notes open from their still-loaded result page, so each keyword's notes
    # run before the next keyword replaces that page.
    second_search = [i for i, op in enumerate(operations) if op == 'search'][1]
    assert operations[:2] == ['search', 'detail'] and second_search > 1
