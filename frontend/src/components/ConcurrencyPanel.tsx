import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Zap } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { ErrorNote } from "@/components/ui/state";
import { api } from "@/lib/api";
import type { Seat } from "@/lib/types";

export function ConcurrencyPanel({ eventId, seats }: { eventId: string; seats: Seat[] }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [seatId, setSeatId] = useState("");
  const available = seats.filter((seat) => seat.status === "AVAILABLE");
  const target = available.some((seat) => seat.id === seatId) ? seatId : (available[0]?.id ?? "");

  const simulate = useMutation({
    mutationFn: () => api.simulate(eventId, target),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["seat-map"] }),
  });

  return (
    <Card>
      <CardContent className="grid gap-3 pt-6">
        <button
          type="button"
          className="flex items-center gap-2 text-left text-sm font-semibold"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <Zap className="h-4 w-4 text-amber-600" /> Concurrency demo: 20 users, 1 seat
        </button>
        {open && (
          <div className="grid gap-3 text-sm">
            <p className="text-muted-foreground">
              Twenty simulated users try to hold the same seat at the same instant. Exactly one
              should win. The winning hold is released afterwards.
            </p>
            <Select
              aria-label="Seat to contest"
              value={target}
              onChange={(e) => setSeatId(e.target.value)}
            >
              {available.map((seat) => (
                <option key={seat.id} value={seat.id}>
                  Seat {seat.seat_label}
                </option>
              ))}
            </Select>
            <Button
              onClick={() => simulate.mutate()}
              disabled={simulate.isPending || target === ""}
            >
              {simulate.isPending ? "Racing…" : "Run race"}
            </Button>
            {available.length === 0 && (
              <p className="text-muted-foreground">No available seats to race for.</p>
            )}
            {simulate.error && <ErrorNote error={simulate.error} />}
            {simulate.data && (
              <dl className="grid grid-cols-2 gap-2 rounded-md bg-secondary p-3">
                {Object.entries(simulate.data.status_counts).map(([status, count]) => (
                  <div key={status}>
                    <dt className="text-xs text-muted-foreground">HTTP {status}</dt>
                    <dd className="font-semibold">{count}</dd>
                  </div>
                ))}
                <div>
                  <dt className="text-xs text-muted-foreground">Double bookings</dt>
                  <dd className="font-semibold">{simulate.data.double_bookings}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Active rows for the seat</dt>
                  <dd className="font-semibold">{simulate.data.active_seat_rows}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Duration</dt>
                  <dd className="font-semibold">{simulate.data.duration_ms} ms</dd>
                </div>
              </dl>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
