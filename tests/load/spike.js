// 10x normal load for 60 seconds, then a cooldown at normal load.
//
// The assertion is on the cooldown, not the spike: shedding load under 10x is
// the graceful degradation being tested, so a threshold on the spike scenario
// would fail the run for behaving correctly. What must hold is that the service
// comes back — the cooldown's error rate and p(95) return to baseline — and that
// the pod never restarted, which scripts/run-load.py checks against the kubelet.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://player-projections:8001';

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-arrival-rate',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 100,
      maxVUs: 500,
      stages: [
        { target: 500, duration: '10s' },
        { target: 500, duration: '60s' },
      ],
    },
    cooldown: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '60s',
      startTime: '70s',
      preAllocatedVUs: 30,
      maxVUs: 100,
    },
  },
  thresholds: {
    // Scoped to the cooldown by k6's automatic `scenario` tag. The spike
    // scenario deliberately carries no threshold. The cooldown p(95) ceiling
    // arrives in Task 3.
    'http_req_failed{scenario:cooldown}': ['rate<0.01'],
  },
};

export default function () {
  const res = http.get(`${TARGET}/projections`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
