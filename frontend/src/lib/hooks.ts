import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";
import { api, ApiError, demoToolsStore } from "./api";
import type { EventsParams } from "./types";

export const queryKeys = {
  me: ["me"] as const,
  events: (params: EventsParams) => ["events", params] as const,
  seatMap: (eventId: string) => ["seat-map", eventId] as const,
  bookings: ["bookings"] as const,
  analytics: (eventId: string) => ["analytics", eventId] as const,
  organizers: ["organizers"] as const,
};

export function useDemoTools(): boolean {
  return useSyncExternalStore(demoToolsStore.subscribe, demoToolsStore.getSnapshot);
}

/** The signed-in user, or null when signed out (a 401 is an answer, not an error). */
export function useMe() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: async () => {
      try {
        return await api.me();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null;
        throw error;
      }
    },
    staleTime: 60_000,
    retry: false,
  });
}

export function useEvents(params: EventsParams) {
  return useQuery({
    queryKey: queryKeys.events(params),
    queryFn: () => api.events(params),
    refetchInterval: 15_000,
  });
}

export function useSeatMap(eventId: string) {
  return useQuery({
    queryKey: queryKeys.seatMap(eventId),
    queryFn: () => api.seatMap(eventId),
    refetchInterval: 5_000,
  });
}

export function useMyBookings(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.bookings,
    queryFn: api.myBookings,
    enabled,
    refetchInterval: 20_000,
  });
}

export function useAnalytics(eventId: string | null) {
  return useQuery({
    queryKey: queryKeys.analytics(eventId ?? "none"),
    queryFn: () => api.analytics(eventId ?? ""),
    enabled: eventId !== null,
  });
}

export function useOrganizers(enabled: boolean) {
  return useQuery({ queryKey: queryKeys.organizers, queryFn: api.organizers, enabled });
}

/**
 * Seconds left on a hold. Anchored to the server's `remaining_seconds` and advanced with
 * performance.now(), which is monotonic, so changing the device clock cannot extend a hold.
 * Re-syncs with the server whenever the window regains focus (timers are throttled in background).
 */
export function useCountdown(remainingSeconds: number | null, resync: () => void): number | null {
  const [anchor, setAnchor] = useState({ at: performance.now(), seconds: remainingSeconds });
  const [now, setNow] = useState(performance.now());

  useEffect(() => {
    setAnchor({ at: performance.now(), seconds: remainingSeconds });
    setNow(performance.now());
  }, [remainingSeconds]);

  useEffect(() => {
    if (remainingSeconds === null) return;
    const tick = window.setInterval(() => setNow(performance.now()), 250);
    window.addEventListener("focus", resync);
    return () => {
      window.clearInterval(tick);
      window.removeEventListener("focus", resync);
    };
  }, [remainingSeconds, resync]);

  if (anchor.seconds === null) return null;
  return Math.max(0, Math.ceil(anchor.seconds - (now - anchor.at) / 1000));
}

export function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
