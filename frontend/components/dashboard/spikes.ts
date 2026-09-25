/**
 * Spike detection for the activity charts.
 *
 * One rule, in one place, so the bars, the tooltip and the list under the
 * chart can never disagree about what counts as a spike.
 *
 * A bucket (a day, or an hour) is a VOLUME spike when it holds at least
 * MIN_SPIKE_CALLS calls AND sits more than two standard deviations above the
 * window's mean. The floor stops a workspace that made one call on a quiet
 * day from "spiking" at a single call; the deviation test is what makes the
 * bar relative to this workspace's own normal rather than to a number picked
 * for somebody else's traffic.
 *
 * It is a FAILURE spike when the bucket has at least MIN_FAIL_CALLS failures
 * and they are at least FAIL_SHARE of that bucket's calls -- a bad hour can
 * be a small one, which the volume test would never notice.
 *
 * Nothing here is estimated: the mean and deviation are computed over exactly
 * the buckets the chart draws, zeros included, because a quiet day is part
 * of what "normal" means.
 */

export const MIN_SPIKE_CALLS = 3;
export const MIN_FAIL_CALLS = 3;
export const FAIL_SHARE = 0.3;
const SIGMAS = 2;

/** One day or one hour, already split by outcome. */
export interface Bucket {
  /** Stable identity: the ISO date for a day, the ISO instant for an hour. */
  key: string;
  ok: number;
  error: number;
  total: number;
}

export type SpikeKind = "volume" | "failures" | "both";

export interface Spike {
  index: number;
  kind: SpikeKind;
  /** total / mean, or 0 when the mean is 0. */
  ratio: number;
}

export interface Analysis {
  total: number;
  errors: number;
  mean: number;
  sigma: number;
  /** The volume line: mean + 2 sigma. Only meaningful when there is data. */
  threshold: number;
  peak: number;
  /** Index of the busiest bucket, or -1 when nothing was called. */
  peakIndex: number;
  failRate: number;
  spikes: Spike[];
}

export function toBucket(key: string, ok: number, error: number): Bucket {
  return { key: key, ok: ok, error: error, total: ok + error };
}

export function analyse(buckets: Bucket[]): Analysis {
  const n = buckets.length;
  let total = 0;
  let errors = 0;
  let peak = 0;
  let peakIndex = -1;
  buckets.forEach(function tally(bucket, index) {
    total += bucket.total;
    errors += bucket.error;
    if (bucket.total > peak) {
      peak = bucket.total;
      peakIndex = index;
    }
  });

  const mean = n === 0 ? 0 : total / n;
  let variance = 0;
  buckets.forEach(function spread(bucket) {
    variance += (bucket.total - mean) * (bucket.total - mean);
  });
  const sigma = n === 0 ? 0 : Math.sqrt(variance / n);
  const threshold = mean + SIGMAS * sigma;

  const spikes: Spike[] = [];
  buckets.forEach(function classify(bucket, index) {
    const volume = bucket.total >= MIN_SPIKE_CALLS && bucket.total > threshold;
    const failing =
      bucket.error >= MIN_FAIL_CALLS &&
      bucket.total > 0 &&
      bucket.error / bucket.total >= FAIL_SHARE;
    if (!volume && !failing) {
      return;
    }
    spikes.push({
      index: index,
      kind: volume && failing ? "both" : volume ? "volume" : "failures",
      ratio: mean > 0 ? bucket.total / mean : 0,
    });
  });

  return {
    total: total,
    errors: errors,
    mean: mean,
    sigma: sigma,
    threshold: threshold,
    peak: peak,
    peakIndex: peakIndex,
    // Whole percent: a rate quoted to two decimals over a handful of calls is
    // precision the number does not have.
    failRate: total > 0 ? Math.round((errors / total) * 100) : 0,
    spikes: spikes,
  };
}

/**
 * A round top for the y axis: the smallest multiple of a step at or above
 * `value`, where the step is always a multiple of 4. That is what keeps the
 * middle and quarter gridlines on whole numbers -- an axis that labels a
 * count of calls "2.5" is wrong about what it is counting.
 */
export function niceCeiling(value: number): number {
  if (value <= 4) {
    return 4;
  }
  if (value <= 40) {
    return Math.ceil(value / 4) * 4;
  }
  const step = 4 * Math.pow(10, Math.floor(Math.log10(value)) - 1);
  return Math.ceil(value / step) * step;
}

export function plural(n: number, one: string, many: string): string {
  return String(n) + " " + (n === 1 ? one : many);
}
