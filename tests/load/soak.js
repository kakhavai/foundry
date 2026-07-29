// 50 RPS held for SOAK_MINUTES. A soak catches faults proportional to total
// requests served — leaked memory, unclosed connections, an unbounded cache —
// rather than to peak concurrency.
//
// It is provably uninformative today and that is written down rather than
// implied: /projections in stub mode reads a module-level dict, filters an empty
// list, and returns JSON, and _poll_loop returns immediately without a snapshot
// URL (player-projections/main.py:54-55), so there is no background task and
// nothing to accumulate. It becomes informative when three ~45 KB documents are
// cached and a poll loop mutates _state every 15 minutes while readers read.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://player-projections:8001';
const MINUTES = __ENV.SOAK_MINUTES || '5';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: `${MINUTES}m`,
      preAllocatedVUs: 30,
      maxVUs: 100,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const res = http.get(`${TARGET}/projections`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
