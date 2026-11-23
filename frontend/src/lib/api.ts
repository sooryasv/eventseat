import type {
  Analytics,
  ApiErrorEnvelope,
  Booking,
  ConfirmResponse,
  EventCreate,
  EventList,
  EventOut,
  EventsParams,
  HoldResponse,
  MyBookings,
  Role,
  SeatMap,
  Simulation,
  SimulatedOutcome,
  User,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiResult<T> {
  data: T;
  replayed: boolean;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  headers?: Record<string, string>;
}

function isEnvelope(payload: unknown): payload is ApiErrorEnvelope {
  if (typeof payload !== "object" || payload === null || !("error" in payload)) return false;
  const { error } = payload as { error: unknown };
  return typeof error === "object" && error !== null && "code" in error && "message" in error;
}

function toApiError(status: number, payload: unknown): ApiError {
  if (isEnvelope(payload)) {
    const { code, message, request_id } = payload.error;
    return new ApiError(status, code, message, request_id);
  }
  return new ApiError(status, "UNKNOWN_ERROR", `Request failed (${status})`, "unknown");
}

let demoToolsEnabled = false;
const demoListeners = new Set<() => void>();

export const demoToolsStore = {
  subscribe: (listener: () => void) => {
    demoListeners.add(listener);
    return () => {
      demoListeners.delete(listener);
    };
  },
  getSnapshot: () => demoToolsEnabled,
};

// Tokens live only in the HttpOnly cookie; this client never reads or stores one.
async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
  const { method = "GET", body, headers = {} } = options;
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      credentials: "same-origin",
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Cannot reach the server. Is the API running?", "n/a");
  }
  const demoHeader = response.headers.get("X-Demo-Tools");
  if (demoHeader !== null) {
    const enabled = demoHeader === "true";
    if (enabled !== demoToolsEnabled) {
      demoToolsEnabled = enabled;
      demoListeners.forEach((listener) => listener());
    }
  }
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) throw toApiError(response.status, payload);
  return { data: payload as T, replayed: response.headers.get("Idempotent-Replayed") === "true" };
}

async function data<T>(path: string, options?: RequestOptions): Promise<T> {
  return (await request<T>(path, options)).data;
}

function query(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  me: () => data<User>("/auth/me"),
  login: (email: string, password: string) =>
    data<User>("/auth/login", { method: "POST", body: { email, password } }),
  register: (email: string, password: string, full_name: string, role: "ATTENDEE" | "ORGANIZER") =>
    data<User>("/auth/register", { method: "POST", body: { email, password, full_name, role } }),
  demoLogin: (persona: Role) =>
    data<User>("/auth/demo-login", { method: "POST", body: { persona } }),
  logout: () => data<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  events: (params: EventsParams) => data<EventList>(`/events${query(params)}`),
  createEvent: (payload: EventCreate) =>
    data<EventOut>("/events", { method: "POST", body: payload }),
  seatMap: (eventId: string) => data<SeatMap>(`/events/${eventId}/seats`),
  analytics: (eventId: string) => data<Analytics>(`/events/${eventId}/analytics`),
  cancelEvent: (eventId: string) =>
    data<{ event_id: string; status: string }>(`/events/${eventId}/cancel`, { method: "POST" }),
  simulate: (eventId: string, seatId: string) =>
    data<Simulation>(`/events/${eventId}/simulate-concurrency`, {
      method: "POST",
      body: { seat_id: seatId },
    }),

  // hold/confirm return ApiResult so the UI can show when an answer was an idempotent replay.
  hold: (eventId: string, seatIds: string[], key: string) =>
    request<HoldResponse>("/bookings/hold", {
      method: "POST",
      body: { event_id: eventId, seat_ids: seatIds },
      headers: { "Idempotency-Key": key },
    }),
  confirm: (bookingId: string, outcome: SimulatedOutcome, key: string) =>
    request<ConfirmResponse>(`/bookings/${bookingId}/confirm`, {
      method: "POST",
      body: { simulate_outcome: outcome },
      headers: { "Idempotency-Key": key },
    }),
  cancelBooking: (bookingId: string) =>
    data<{ booking_id: string; status: string }>(`/bookings/${bookingId}/cancel`, {
      method: "POST",
    }),
  myBookings: () => data<MyBookings>("/bookings/me"),

  organizers: () => data<User[]>("/admin/organizers"),
  verifyOrganizer: (userId: string, isVerified: boolean) =>
    data<User>(`/admin/organizers/${userId}/verify`, {
      method: "PATCH",
      body: { is_verified: isVerified },
    }),
};

export type { Booking };
