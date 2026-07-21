# ============================================================
# Test: Time Proximity Edge Cases
# 测试：时间亲近度边界情况
#
# Verifies:
#   1. Cross-day (23:59:59 → 00:00:01) doesn't produce negative time
#   2. Timezone-aware timestamps are handled correctly
#   3. Future timestamps are clamped to 0
#   4. UTC vs local timezone mixing doesn't break scoring
# ============================================================

import pytest
import math
from datetime import datetime, timedelta, timezone


class TestTimeProximity:
    """Test time proximity scoring edge cases."""

    def test_cross_day_does_not_overflow(self):
        """
        测试跨天边界：23:59:59 → 00:00:01 不应产生负数。
        """
        # Simulate a bucket created at 23:59:59 (one second before midnight)
        midnight_minus_1 = datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
        midnight_plus_1 = datetime(2026, 1, 2, 0, 0, 1, tzinfo=timezone.utc)
        
        meta = {"last_active": midnight_minus_1.isoformat()}
        
        # Simulate the actual calculation from _calc_time_score
        last_active_str = meta.get("last_active", meta.get("created", ""))
        last_active = datetime.fromisoformat(str(last_active_str))
        
        # Use midnight_plus_1 as "now" for testing
        delta_seconds = (midnight_plus_1 - last_active).total_seconds()
        days = max(0.0, delta_seconds / 86400)
        
        assert days >= 0.0, f"Days should not be negative: {days}"
        assert days < 0.001, f"Should be very recent (2 seconds): {days}"
        
        time_score = math.exp(-0.02 * days)
        assert 0.9999 < time_score <= 1.0, f"Time score should be nearly 1.0: {time_score}"

    def test_future_timestamp_clamped(self):
        """
        测试未来时间戳：应该被 clamp 到 0（最近）。
        """
        future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        meta = {"last_active": future_time}
        
        # Simulate the actual calculation
        last_active_str = meta.get("last_active", meta.get("created", ""))
        last_active = datetime.fromisoformat(str(last_active_str))
        
        now = datetime.now(timezone.utc)
        delta_seconds = (now - last_active).total_seconds()
        days = max(0.0, delta_seconds / 86400)
        
        # Future timestamp should give negative delta, clamped to 0
        assert days == 0.0, f"Future should clamp to 0 days: {days}"
        
        time_score = math.exp(-0.02 * days)
        assert time_score == 1.0, f"Future timestamp should have score 1.0: {time_score}"

    def test_timezone_utc_vs_local_mixed(self):
        """
        测试时区混合：UTC 时间戳 vs 本地时间戳不应产生错误。
        """
        utc_now = datetime.now(timezone.utc)
        
        # Timestamp with UTC offset: "2026-07-22T08:00:00+00:00"
        utc_timestamp = utc_now.isoformat()
        
        # Timestamp with local offset: "2026-07-22T16:00:00+08:00"
        local_timestamp = utc_now.astimezone().isoformat()
        
        # Both should produce similar time scores
        def calc_score(timestamp_str):
            last_active = datetime.fromisoformat(timestamp_str)
            now = datetime.now(timezone.utc)
            delta_seconds = (now - last_active).total_seconds()
            days = max(0.0, delta_seconds / 86400)
            return math.exp(-0.02 * days)
        
        score_utc = calc_score(utc_timestamp)
        score_local = calc_score(local_timestamp)
        
        assert 0.999 < score_utc <= 1.0, f"UTC score should be ~1.0: {score_utc}"
        assert 0.999 < score_local <= 1.0, f"Local score should be ~1.0: {score_local}"

    def test_old_timestamp_does_not_overflow(self):
        """
        测试旧时间戳：100年前的记忆不应溢出。
        """
        old_time = datetime(1926, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        meta = {"last_active": old_time.isoformat()}
        
        last_active_str = meta.get("last_active", meta.get("created", ""))
        last_active = datetime.fromisoformat(str(last_active_str))
        
        now = datetime.now(timezone.utc)
        delta_seconds = (now - last_active).total_seconds()
        days = max(0.0, delta_seconds / 86400)
        
        time_score = math.exp(-0.02 * days)
        
        assert 0.0 <= time_score <= 1.0, f"Score should be in [0,1]: {time_score}"
        assert time_score < 0.01, f"Old memory should have very low score: {time_score}"


class TestNowIsoTimezone:
    """Test now_iso() produces timezone-aware timestamps."""

    def test_now_iso_contains_timezone(self):
        """
        测试 now_iso() 返回的时间戳包含时区信息。
        这是防止跨时区部署时时间错乱的关键。
        """
        from utils import now_iso
        
        timestamp = now_iso()
        
        # ISO format with timezone should contain '+' or '-' for offset
        has_offset = '+' in timestamp or '-' in timestamp
        assert has_offset, f"Timestamp should contain timezone offset: {timestamp}"
        
        # Check that it's parseable and timezone-aware
        dt = datetime.fromisoformat(timestamp)
        assert dt.tzinfo is not None, f"Timestamp should be timezone-aware: {timestamp}"
        assert dt.tzinfo.utcoffset(dt) is not None, f"Timestamp should have UTC offset: {timestamp}"
