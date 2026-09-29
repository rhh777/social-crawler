"""Synthetic protocol pages. Never imports or initializes a real transport."""

from social_crawler.adapters.douyin.http import parse_page as parse_douyin
from social_crawler.adapters.xhs.parsing import parse_page as parse_xhs
from social_crawler.adapters.xhs.sites import is_xhs_platform
from social_crawler.domain.models import Operation


class FixtureAdapter:
    version = "synthetic-protocol-fixtures-v1"

    def __init__(self, platform, budget, samples):
        self.platform, self.budget, self.samples = platform, budget, samples

    async def fetch(self, request):
        await self.budget.admit(str(request.operation))
        is_xhs = is_xhs_platform(self.platform)
        page = int(request.context.get("page", request.context.get("cursor", "0") or 0))
        if request.operation == Operation.SEARCH:
            first = page <= (1 if is_xhs else 0)
            ids = ["synthetic-1", "synthetic-2"] if first else ["synthetic-2", "synthetic-3"]
            raws = [self._content(cid) for cid in ids]
            data = (
                {
                    "items": [
                        {
                            "id": r["note_id"],
                            "model_type": "note",
                            "xsec_token": "fixture-token",
                            "note_card": r,
                        }
                        for r in raws
                    ],
                    "has_more": first,
                }
                if is_xhs
                else {
                    "data": [{"aweme_info": r} for r in raws],
                    "has_more": int(first),
                    "cursor": 1 if first else 2,
                    "log_pb": {"impr_id": "fixture-search"},
                }
            )
        elif request.operation == Operation.DETAIL:
            raw = self._content(request.content_id)
            data = (
                {"items": [{"id": request.content_id, "note_card": raw}]}
                if is_xhs
                else {"aweme_detail": raw}
            )
        else:
            root = request.root_id or request.content_id
            first = page == 0
            ids = [root + "-1", root + "-2"] if first else [root + "-2", root + "-3"]
            raw = [self._comment(cid, request.operation == Operation.COMMENTS) for cid in ids]
            data = {
                "comments": raw,
                "has_more": first if is_xhs else int(first),
                "cursor": "1" if first else "2",
            }
        payload = (
            {"success": True, "code": 0, "data": data} if is_xhs else {"status_code": 0} | data
        )
        sample = self.samples.write(
            {
                "synthetic": True,
                "operation": str(request.operation),
                "content_id": request.content_id,
                "root_id": request.root_id,
                "context": request.context,
            },
            200,
            payload,
        )
        result = (
            parse_xhs(request, payload, platform=self.platform)
            if is_xhs
            else parse_douyin(request, payload)
        )
        result.sample_ref = sample
        return result

    def _content(self, cid):
        if is_xhs_platform(self.platform):
            return {
                "note_id": cid,
                "title": "合成标题 " + cid,
                "desc": "仅供离线测试的正文",
                "time": 1_700_000_000_000,
                "user": {"user_id": "synthetic-author", "nickname": "离线作者"},
                "interact_info": {"comment_count": 3},
                "image_list": [],
            }
        return {
            "aweme_id": cid,
            "desc": "仅供离线测试的正文 " + cid,
            "create_time": 1_700_000_000,
            "author": {"uid": "synthetic-author", "nickname": "离线作者"},
            "statistics": {"comment_count": 3},
        }

    def _comment(self, cid, is_root):
        if is_xhs_platform(self.platform):
            return {
                "id": cid,
                "content": "合成评论 " + cid,
                "create_time": 1_700_000_000_000,
                "user_info": {"user_id": "synthetic-commenter"},
                "sub_comment_count": 3 if is_root else 0,
                "sub_comment_has_more": True if is_root else False,
                "sub_comments": [self._comment(cid + "-1", False)] if is_root else [],
            }
        return {
            "cid": cid,
            "text": "合成评论 " + cid,
            "create_time": 1_700_000_000,
            "user": {"uid": "synthetic-commenter"},
            "reply_comment_total": 3 if is_root else 0,
            "reply_comment": [self._comment(cid + "-1", False)] if is_root else [],
        }

    async def close(self):
        pass


def submit_fixture(app, body):
    """Seed console integration tests through the real worker pool, without platform traffic."""
    import asyncio

    from social_crawler.adapters.samples import Samples
    from social_crawler.domain.models import RunConfig
    from social_crawler.environments.session import EnvironmentConfig, Session, exclusive
    from social_crawler.orchestration.report import export_run
    from social_crawler.orchestration.worker import run_worker

    config = RunConfig.model_validate(body["config"] | {"min_interval": 0})
    session = Session(EnvironmentConfig(account_ref="fixture"), config.platform, offline=True)
    with app.store() as store:
        run_id = store.create_run(config, mode="offline", binding=session.binding)
    keys = app.keys(session, run_id=run_id, offline=True)

    def work():
        with app.store() as store, exclusive(keys):
            directory = app.root / "data/validation" / run_id
            asyncio.run(run_worker(
                store, run_id,
                lambda budget: FixtureAdapter(config.platform, budget, Samples(directory)),
                session=session, preserve_cancel=True,
            ))
            return export_run(store, run_id, directory)

    try:
        return app.launch_collection(config.platform, run_id, keys, work)
    except Exception as exc:
        app.fail_pending_collection(run_id, exc)
        raise
