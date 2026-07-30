// Climbs in rungs until the error rate crosses 1%, then aborts.
//
// This shape MEASURES; it does not assert. Reaching 1% errors is its output, and
// scripts/run-load.py accepts exit code 99 from it for that reason. Because it
// cannot fail, it is not coverage — it produces a number for
// docs/scale-baselines.md and nothing more.
//
// One scenario per rung, rather than one ramping scenario, so the abort names
// the rate: k6's abort message quotes the crossed threshold, and each threshold
// is scoped to its rung's scenario tag. --summary-mode full then prints every
// rung's error rate beside it.
import http from 'k6/http';

const TARGET = __ENV.TARGET || 'http://player-projections:8001';

const RUNGS = [50, 100, 200, 300, 400, 600, 800];
const RUNG_SECONDS = 30;

const scenarios = {};
const thresholds = {};
RUNGS.forEach((rate, i) => {
  const name = `rate_${String(rate).padStart(3, '0')}`;
  scenarios[name] = {
    executor: 'constant-arrival-rate',
    rate: rate,
    timeUnit: '1s',
    duration: `${RUNG_SECONDS}s`,
    startTime: `${i * RUNG_SECONDS}s`,
    preAllocatedVUs: 50,
    maxVUs: 800,
  };
  // abortOnFail stops the whole test at the first rung that breaks, so the
  // rungs above it never run and the abort message names the rate that broke.
  thresholds[`http_req_failed{scenario:${name}}`] = [
    { threshold: 'rate<0.01', abortOnFail: true, delayAbortEval: '5s' },
  ];
});

export const options = { scenarios, thresholds };

export default function () {
  http.get(`${TARGET}/projections`);
}
