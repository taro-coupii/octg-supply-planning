export function writeParam(
  params: URLSearchParams,
  key: string,
  value: string | null
): URLSearchParams {
  const next = new URLSearchParams(params);
  if (value === null || value === "") next.delete(key);
  else next.set(key, value);
  return next;
}

export function readListParam(params: URLSearchParams, key: string): string[] {
  return params.getAll(key);
}

export function writeListParam(
  params: URLSearchParams,
  key: string,
  values: string[]
): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete(key);
  for (const v of values) next.append(key, v);
  return next;
}
