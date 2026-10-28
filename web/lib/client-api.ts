export class HttpError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Request failed (${status})`);
    this.name = "HttpError";
    this.status = status;
  }
}

export async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) throw new HttpError(response.status);
  return (await response.json()) as T;
}

export async function fetchOptionalJson<T>(
  url: string,
  options?: RequestInit,
): Promise<T | null> {
  try {
    return await fetchJson<T>(url, options);
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return null;
    throw error;
  }
}
