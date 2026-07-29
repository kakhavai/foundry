// 0 → 100 RPS over 5 minutes. Measures the latency-versus-load curve and
// establishes the p(95) baseline the other shapes are calibrated against.
//
// player-projections serves {"projections": [], "count": 0} in stub mode, so
// this measures uvicorn and FastAPI overhead, not the service's real work. See
// docs/scale-baselines.md.
import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://player-projections:8001';

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 200,
      stages: [{ target: 100, duration: '5m' }],
    },
  },
  thresholds: {
    // On principle, not from observation. The p(95) ceiling is calibrated in
    // Task 3 from a captured run.
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const res = http.get(`${TARGET}/projections`);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'body is a projections document': (r) => r.body && r.body.includes('"count"'),
  });
}
