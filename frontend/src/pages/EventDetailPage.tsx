import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CalendarClock, MapPin } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { CheckoutPanel } from "@/components/CheckoutPanel";
import { ConcurrencyPanel } from "@/components/ConcurrencyPanel";
import { SeatGrid } from "@/components/SeatGrid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, ErrorNote, Loading } from "@/components/ui/state";
import { toast } from "@/components/ui/sonner";
import { api } from "@/lib/api";
import {
  formatDateTime,
  queryKeys,
  useDemoTools,
  useMe,
  useMyBookings,
  useSeatMap,
} from "@/lib/hooks";
import { MAX_SEATS, type Booking } from "@/lib/types";

export function EventDetailPage() {
  const { eventId = "" } = useParams();
  const queryClient = useQueryClient();
  const { data: user } = useMe();
  const demoTools = useDemoTools();
  const seatMap = useSeatMap(eventId);
  const bookings = useMyBookings(Boolean(user));
  const [selected, setSelected] = useState<string[]>([]);
  const [stored, setStored] = useState<Booking | null>(null);
  // The same selection re-sent after a network failure re-uses its key; a new selection gets a new one.
  const holdKey = useRef<{ selection: string; key: string } | null>(null);

  const userId = user?.id ?? null;
  useEffect(() => {
    setStored(null);
    setSelected([]);
    holdKey.current = null;
  }, [userId]);

  const liveSeats = seatMap.data?.seats;
  useEffect(() => {
    if (!liveSeats) return;
    const available = new Set(
      liveSeats.filter((seat) => seat.status === "AVAILABLE").map((seat) => seat.id),
    );
    setSelected((current) => {
      const next = current.filter((id) => available.has(id));
      if (next.length === current.length) return current;
      holdKey.current = null;
      return next;
    });
  }, [liveSeats]);

  const activeHold = bookings.data?.holds.find((h) => h.event_id === eventId) ?? null;
  useEffect(() => {
    if (activeHold) setStored(activeHold);
  }, [activeHold]);
  const checkoutHold = activeHold ?? stored;

  const hold = useMutation({
    mutationFn: () => {
      const selection = [...selected].sort().join(",");
      if (holdKey.current?.selection !== selection) {
        holdKey.current = { selection, key: crypto.randomUUID() };
      }
      return api.hold(eventId, selected, holdKey.current.key);
    },
    onSuccess: ({ data }) => {
      toast.success(`Seats ${data.seat_labels.join(", ")} held`);
      setSelected([]);
      holdKey.current = null;
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.bookings });
      void queryClient.invalidateQueries({ queryKey: queryKeys.seatMap(eventId) });
    },
  });

  const toggle = (seatId: string) => {
    if (hold.error) hold.reset();
    setSelected((current) => {
      if (current.includes(seatId)) return current.filter((id) => id !== seatId);
      return current.length >= MAX_SEATS ? current : [...current, seatId];
    });
  };

  if (seatMap.isLoading) return <Loading label="Loading seat map…" />;
  if (seatMap.error) return <ErrorNote error={seatMap.error} />;
  if (!seatMap.data) return <Empty>Event not found.</Empty>;

  const { event, seats } = seatMap.data;
  const cancelled = event.status === "CANCELLED";
  const closed = new Date(event.booking_closes_at).getTime() <= Date.now();
  const canHold = Boolean(user) && !cancelled && !closed && checkoutHold === null;
  const labelFor = (id: string) => seats.find((seat) => seat.id === id)?.seat_label ?? id;

  return (
    <div className="grid gap-5">
      <Link
        to="/"
        className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" /> All events
      </Link>
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-bold">{event.title}</h1>
          <Badge variant="secondary">{event.category}</Badge>
          {cancelled && <Badge variant="destructive">Cancelled</Badge>}
        </div>
        <p className="mt-1 flex flex-wrap items-center gap-x-4 text-sm text-muted-foreground">
          <span className="flex items-center gap-1">
            <MapPin className="h-4 w-4" /> {event.venue}
          </span>
          <span className="flex items-center gap-1">
            <CalendarClock className="h-4 w-4" /> {formatDateTime(event.starts_at)}
          </span>
          <span>Booking closes {formatDateTime(event.booking_closes_at)}</span>
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-[1fr_20rem]">
        <Card>
          <CardContent className="pt-6">
            <SeatGrid seats={seats} selected={selected} onToggle={toggle} disabled={!canHold} />
          </CardContent>
        </Card>

        <div className="grid content-start gap-4">
          {checkoutHold && (
            <CheckoutPanel
              key={checkoutHold.id}
              hold={checkoutHold}
              onClose={() => setStored(null)}
            />
          )}
          {!checkoutHold && (
            <Card>
              <CardHeader>
                <CardTitle>Your selection</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-3 text-sm">
                {!user && (
                  <p className="text-muted-foreground">
                    {demoTools
                      ? "Sign in (try a demo login) to hold seats."
                      : "Sign in to hold seats."}
                  </p>
                )}
                {cancelled && <p className="text-muted-foreground">This event was cancelled.</p>}
                {closed && !cancelled && (
                  <p className="text-muted-foreground">Booking for this event has closed.</p>
                )}
                {canHold && (
                  <>
                    <p>
                      {selected.length === 0
                        ? `Choose up to ${MAX_SEATS} seats.`
                        : `Seats: ${selected.map(labelFor).join(", ")}`}
                    </p>
                    {hold.error && <ErrorNote error={hold.error} />}
                    <Button
                      onClick={() => hold.mutate()}
                      disabled={selected.length === 0 || hold.isPending}
                    >
                      {hold.isPending ? "Holding…" : "Hold for 5 minutes"}
                    </Button>
                  </>
                )}
              </CardContent>
            </Card>
          )}
          {user && !cancelled && demoTools && <ConcurrencyPanel eventId={eventId} seats={seats} />}
        </div>
      </div>
    </div>
  );
}
