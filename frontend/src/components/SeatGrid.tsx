import { cn } from "@/components/ui/cn";
import type { Seat, SeatStatus } from "@/lib/types";

const STATUS_STYLE: Record<SeatStatus, string> = {
  AVAILABLE: "bg-emerald-100 text-emerald-900 hover:bg-emerald-200 border-emerald-300",
  HELD: "bg-amber-100 text-amber-900 border-amber-300 cursor-not-allowed",
  BOOKED: "bg-slate-300 text-slate-600 border-slate-400 cursor-not-allowed",
  MINE: "bg-blue-100 text-blue-900 border-blue-400 cursor-not-allowed",
};

const LEGEND: { status: SeatStatus; label: string }[] = [
  { status: "AVAILABLE", label: "Available" },
  { status: "HELD", label: "Held by someone" },
  { status: "BOOKED", label: "Booked" },
  { status: "MINE", label: "Yours" },
];

interface SeatGridProps {
  seats: Seat[];
  selected: string[];
  onToggle: (seatId: string) => void;
  disabled: boolean;
}

export function SeatGrid({ seats, selected, onToggle, disabled }: SeatGridProps) {
  const rows = new Map<string, Seat[]>();
  for (const seat of seats) rows.set(seat.row_label, [...(rows.get(seat.row_label) ?? []), seat]);

  return (
    <div className="grid gap-4">
      <div className="mx-auto w-3/4 rounded-b-xl bg-slate-800 py-1 text-center text-xs tracking-widest text-slate-100">
        STAGE
      </div>
      <div className="grid gap-2 overflow-x-auto pb-1" role="group" aria-label="Seat map">
        {[...rows.entries()].map(([rowLabel, rowSeats]) => (
          <div key={rowLabel} className="flex items-center justify-center gap-1.5">
            <span className="w-5 text-xs font-medium text-muted-foreground">{rowLabel}</span>
            {rowSeats.map((seat) => {
              const isSelected = selected.includes(seat.id);
              const selectable = seat.status === "AVAILABLE" && !disabled;
              return (
                <button
                  key={seat.id}
                  type="button"
                  disabled={!selectable}
                  aria-pressed={isSelected}
                  aria-label={`Seat ${seat.seat_label}, ${seat.status.toLowerCase()}`}
                  title={`${seat.seat_label} — ${seat.status}`}
                  onClick={() => onToggle(seat.id)}
                  className={cn(
                    "h-9 w-9 rounded-md border text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    STATUS_STYLE[seat.status],
                    isSelected &&
                      "bg-primary text-primary-foreground ring-2 ring-primary ring-offset-1 hover:bg-primary",
                  )}
                >
                  {seat.seat_number}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <ul className="flex flex-wrap justify-center gap-4 text-xs text-muted-foreground">
        {LEGEND.map(({ status, label }) => (
          <li key={status} className="flex items-center gap-1.5">
            <span className={cn("h-3.5 w-3.5 rounded-sm border", STATUS_STYLE[status])} />
            {label}
          </li>
        ))}
        <li className="flex items-center gap-1.5">
          <span className="h-3.5 w-3.5 rounded-sm bg-primary" /> Your selection
        </li>
      </ul>
    </div>
  );
}
