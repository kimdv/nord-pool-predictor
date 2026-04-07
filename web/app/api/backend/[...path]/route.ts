import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.API_BASE_URL ?? "http://localhost:8000";

const ALLOWED_PREFIXES = [
  "prices/",
  "forecasts/",
  "areas",
  "health",
  "ha/",
  "tariffs/",
  "models",
  "jobs",
];

function isAllowedPath(path: string): boolean {
  return ALLOWED_PREFIXES.some((p) => path === p.replace(/\/$/, "") || path.startsWith(p));
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const joined = path.join("/");

  if (!isAllowedPath(joined)) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const search = request.nextUrl.searchParams.toString();
  const url = `${BACKEND}/api/${joined}${search ? `?${search}` : ""}`;

  try {
    const res = await fetch(url, { cache: "no-store" });
    const contentType = res.headers.get("content-type") ?? "";

    if (!contentType.includes("application/json")) {
      return NextResponse.json(
        { error: "Unexpected backend response" },
        { status: 502 },
      );
    }

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Backend unavailable" },
      { status: 502 },
    );
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const joined = path.join("/");

  if (!isAllowedPath(joined)) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const url = `${BACKEND}/api/${joined}`;

  try {
    const res = await fetch(url, { method: "POST", cache: "no-store" });
    const contentType = res.headers.get("content-type") ?? "";

    if (!contentType.includes("application/json")) {
      return NextResponse.json(
        { error: "Unexpected backend response" },
        { status: 502 },
      );
    }

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Backend unavailable" },
      { status: 502 },
    );
  }
}
