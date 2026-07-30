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
    // Calibrated: max(2x the worst p(95) across three captured runs, 10ms).
    // At this service's scale the 10ms floor is what governs — absolute jitter
    // on a contended single-node cluster outweighs the ratio below ~5ms. So
    // this catches an order-of-magnitude regression, not a subtle one, and it
    // is NOT the >20% regression gate, which stays deferred.
    // See docs/scale-baselines.md for the numbers and which term won.
    http_req_duration: ['p(95)<10'],
  },
};

export default function () {
  const res = http.get(`${TARGET}/projections`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
