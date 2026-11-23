// Mirrors backend/app/schemas.py. Keep the two in lockstep.

export type Role = "ATTENDEE" | "ORGANIZER" | "SUPER_ADMIN";
export type Category = "CONFERENCE" | "WORKSHOP" | "MEETUP" | "SEMINAR";
export type EventStatus = "PUBLISHED" | "CANCELLED";
export type BookingStatus = "HELD" | "CONFIRMED" | "CANCELLED" | "EXPIRED";
export type SeatStatus = "AVAILABLE" | "HELD" | "BOOKED" | "MINE";
export type SimulatedOutcome = "SUCCESS" | "FAILURE";

export const CATEGORIES: readonly Category[] = ["CONFERENCE", "WORKSHOP", "MEETUP", "SEMINAR"];
export const MAX_SEATS = 4;

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_verified_organizer: boolean;
}

export interface EventOut {
  id: string;
  organizer_id: string;
  title: string;
  category: Category;
  venue: string;
  starts_at: string;
  booking_closes_at: string;
  status: EventStatus;
}

export interface EventSummary extends EventOut {
  total_seats: number;
  available_seats: number;
}

export interface EventList {
  items: EventSummary[];
  page: number;
  page_size: number;
  total: number;
}

export interface EventsParams {
  q?: string;
  category?: Category;
  page?: number;
  page_size?: number;
}

export interface EventCreate {
  title: string;
  category: Category;
  venue: string;
  starts_at: string;
  booking_closes_at: string;
  rows: number;
  cols: number;
}

export interface Seat {
  id: string;
  seat_label: string;
  row_label: string;
  seat_number: number;
  status: SeatStatus;
}

export interface SeatMap {
  event: EventOut;
  seats: Seat[];
}

export interface RosterEntry {
  booking_id: string;
  full_name: string;
  email: string;
  seat_labels: string[];
  confirmed_at: string;
}

export interface Analytics {
  event_id: string;
  total_seats: number;
  available: number;
  held: number;
  confirmed: number;
  utilization_pct: number;
  roster: RosterEntry[];
}

export interface HoldResponse {
  booking_id: string;
  event_id: string;
  status: BookingStatus;
  seat_ids: string[];
  seat_labels: string[];
  held_until: string;
  remaining_seconds: number;
}

export interface ConfirmResponse {
  booking_id: string;
  event_id: string;
  status: BookingStatus;
  ticket_code: string;
  confirmed_at: string;
}

export interface Booking {
  id: string;
  event_id: string;
  event_title: string;
  event_starts_at: string;
  status: BookingStatus;
  held_until: string | null;
  remaining_seconds: number | null;
  ticket_code: string | null;
  seat_labels: string[];
  created_at: string;
  confirmed_at: string | null;
  cancelled_at: string | null;
}

export interface MyBookings {
  tickets: Booking[];
  holds: Booking[];
  history: Booking[];
}

export interface Simulation {
  status_counts: Record<string, number>;
  double_bookings: number;
  active_seat_rows: number;
  duration_ms: number;
  released_holds: number;
}

export interface ApiErrorEnvelope {
  error: { code: string; message: string; request_id: string };
}
