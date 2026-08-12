# -*- coding: utf-8 -*-
"""
Local reproducible check for the daily review flow (daily_review).
日终整理流程的本地可复现验证脚本（使用隔离临时库，不触碰真实记忆）。

Steps / 步骤:
  1. Prepare: seed 3-4 test memories in an isolated temp bucket store
     (1 expired task + 1 normal + 1 near-duplicate pair, tagged [TEST]).
  2. Call daily_review(record_report=True) — the recommended interface.
  3. Assert: structured result; expired candidate present; no auto delete/merge;
     echo-chamber daily_review report written.
  4. Decision-path mini-check: mark the expired task resolved via the same
     mutation trace() performs, then confirm it disappears from candidates.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import frontmatter as fm


def build_config(tmp: str) -> dict:
    return {
        "buckets_dir": os.path.join(tmp, "buckets"),
        "merge_threshold": 75,
        "matching": {"fuzzy_threshold": 50, "max_results": 10},
        "wikilink": {"enabled": False},
        "decay": {
            "lambda": 0.05, "threshold": 0.3, "check_interval_hours": 24,
            "emotion_weights": {"base": 1.0, "arousal_boost": 0.8},
        },
    }


def check(label: str, cond: bool, extra: str = ""):
    print(f"{'PASS' if cond else 'FAIL'} | {label}{(' — ' + extra) if extra else ''}")
    return cond


async def main():
    tmp = tempfile.mkdtemp(prefix="ombre_daily_review_check_")
    cfg = build_config(tmp)
    for d in ("permanent", "dynamic", "archive", "feel"):
        os.makedirs(os.path.join(cfg["buckets_dir"], d), exist_ok=True)

    from bucket_manager import BucketManager
    from housekeeper import Housekeeper

    bm = BucketManager(cfg)
    hk = Housekeeper(cfg, bm, None, identity_mgr=None)

    print("=" * 70)
    print("STEP 1: seed test memories (isolated temp store)")
    print("=" * 70)

    past = (datetime.now(timezone.utc) - timedelta(days=9)).date()
    future = (datetime.now(timezone.utc) + timedelta(days=5)).date()

    # 1 expired task (past explicit date) + 1 future task (must NOT be flagged)
    expired_id = await bm.create(
        content=f"{past.isoformat()} [TEST] 去银行办卡（测试记忆，可删除）",
        task_flag=True,
    )
    future_id = await bm.create(
        content=f"{future.isoformat()} [TEST] 下周交房租（测试记忆）",
        task_flag=True,
    )
    # 1 normal memory
    normal_id = await bm.create(
        content="[TEST] 午后喝了一杯冰美式，很提神（测试记忆）",
        task_flag=False,
    )
    # near-duplicate pair (identical content, no auto-merge at bucket layer)
    dup_a = await bm.create(content="[TEST] 我每天都会给猫梳毛十分钟（测试记忆）")
    dup_b = await bm.create(content="[TEST] 我每天都会给猫梳毛十分钟（测试记忆）")

    before_ids = {b["id"] for b in await bm.list_all()}
    print(f"seeded ids: expired={expired_id}, future={future_id}, normal={normal_id}, dup_a={dup_a}, dup_b={dup_b}")
    print(f"bucket count before review: {len(before_ids)}")

    print()
    print("=" * 70)
    print("STEP 2: call daily_review(record_report=True)")
    print("=" * 70)
    result = await hk.run_daily_review(record_report=True)

    ok = []
    ok.append(check("structured result returned", result.get("mode") == "daily_review",
                    f"mode={result.get('mode')} review_date={result.get('review_date')}"))

    # 2a. top-level fields
    for field in ("summary_guidance", "candidates", "execution_instructions", "health", "scan_summary"):
        ok.append(check(f"field present: {field}", field in result,
                        "" if field not in result else json.dumps(result[field], ensure_ascii=False)[:160]))

    # 2b. guidance structure
    sg = result.get("summary_guidance", {})
    ok.append(check("guidance has message/suggested_fields/hold_template/journal_status",
                    all(k in sg for k in ("message", "suggested_fields", "hold_template", "journal_status"))))

    # 2c. expired candidate present & reasonable
    cands = result.get("candidates", [])
    expired = [c for c in cands if c.get("category") == "expired_task"]
    expired_ok = bool(expired) and expired[0]["bucket_id"] == expired_id
    ok.append(check("expired test task appears in candidates", expired_ok,
                    json.dumps(expired[0], ensure_ascii=False)[:300] if expired else "no expired candidate"))
    reason_ok = expired and ("已过去" in expired[0]["reason"]) and ("mark_resolved" in expired[0]["options"])
    ok.append(check("expired candidate reason & options sensible", bool(reason_ok),
                    expired[0]["reason"] if expired else ""))

    # future task must NOT be flagged
    flagged = {c["bucket_id"] for c in cands}
    ok.append(check("future-dated task NOT flagged", future_id not in flagged))

    # 2d. execution instructions mention trace
    ok.append(check("execution_instructions mentions trace (main-AI decision)", "trace" in result.get("execution_instructions", "")))

    # 2e. NO auto delete / merge / resolve happened
    after_ids = {b["id"] for b in await bm.list_all()}
    ok.append(check("no bucket auto-deleted", before_ids == after_ids))
    dup_post = fm.load(bm._find_bucket_file(dup_b))
    ok.append(check("duplicate NOT auto-merged (still raw)", not dup_post.get("merged", False)))
    exp_post = fm.load(bm._find_bucket_file(expired_id))
    ok.append(check("expired task NOT auto-resolved", not exp_post.get("resolved", False)))

    # 2f. echo-chamber daily_review report written
    digests = await hk.echo_chamber.get_pending_digests(digest_type="daily_review")
    report_ok = len(digests) == 1 and digests[0]["metadata"]["review_date"] == result["review_date"]
    ok.append(check("echo-chamber daily_review report written", report_ok,
                    f"{len(digests)} report(s)"))

    print()
    print("=" * 70)
    print("STEP 3: main-AI decision path (mark resolved, mirroring trace())")
    print("=" * 70)
    # Main AI chooses mark_resolved + force_resolved (same mutation trace() performs)
    await bm.update(expired_id, resolved=True, force_resolved=True)
    post = fm.load(bm._find_bucket_file(expired_id))
    ok.append(check("bucket side-state changed (resolved=True after trace-equivalent)",
                    post.get("resolved") is True))

    # Re-run review: resolved task must no longer be an expired candidate
    result2 = await hk.run_daily_review(record_report=True)
    expired2 = [c for c in result2.get("candidates", []) if c.get("category") == "expired_task"]
    ok.append(check("resolved task dropped from next review (main-AI decision honored, not locked to default)",
                    expired_id not in {c["bucket_id"] for c in expired2}))

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = sum(1 for _ in ok if _)  # placeholder; counted below
    passed = 0
    for o in ok:
        if o:
            passed += 1
    print(f"passed {passed}/{len(ok)} checks")
    print(f"review_date={result['review_date']} scanned={result['scan_summary']['scanned_buckets']} "
          f"candidates={result['scan_summary']['candidates_total']} "
          f"by_category={result['scan_summary']['candidates_by_category']}")
    health = result.get("health", {})
    print(f"health: errors={health.get('errors', [])} warnings={health.get('warnings', [])}")
    return 0 if passed == len(ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
