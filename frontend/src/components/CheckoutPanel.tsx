import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, Timer } from "lucide-react";
import { useCallback, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorNote } from "@/components/ui/state";
import { toast } from "@/components/ui/sonner";
import { api, type ApiResult } from "@/lib/api";
import { formatClock, queryKeys, useCountdown } from "@/lib/hooks";
import type { Booking, ConfirmResponse, SimulatedOutcome } from "@/lib/types";

interface CheckoutPanelProps {
  hold: Booking;
  onClose: () => void;
}

/**
 * Simulated payment step. The outcome buttons are a demo stand-in for a payment gateway:
 * no card data is collected and nothing is charged.
 */
export function CheckoutPanel({ hold, onClose }: CheckoutPanelProps) {
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState<SimulatedOutcome>("SUCCESS");
  // One key per payment attempt; "Send again" deliberately re-uses it to demonstrate replay.
  const [attemptKey, setAttemptKey] = useState(() => crypto.randomUUID());
  const [result, setResult] = useState<ApiResult<ConfirmResponse> | null>(null);

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.bookings });
    void queryClient.invalidateQueries({ queryKey: ["seat-map"] });
  }, [queryClient]);

  const secondsLeft = useCountdown(hold.remaining_seconds, refresh);
  const expired = secondsLeft === 0;

  const pay = useMutation({
    mutationFn: () => api.confirm(hold.id, outcome, attemptKey),
    onSuccess: (response) => {
      setResult(response);
      toast.success("Booking confirmed");
      refresh();
    },
    onError: (error: Error) => {
      toast.error(error.message);
      // A declined payment is a definitive answer for this key; the next try needs a fresh one.
      refresh();
    },
  });
  const release = useMutation({
    mutationFn: () => api.cancelBooking(hold.id),
    onSuccess: () => {
      toast.success("Hold released");
      refresh();
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const retryWithNewKey = () => {
    pay.reset();
    setAttemptKey(crypto.randomUUID());
  };

  if (result) {
    return (
      <Card className="border-emerald-300">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-emerald-700">
            <CheckCircle2 className="h-5 w-5" /> Booking confirmed
          </CardTitle>
          <CardDescription>Seats {hold.seat_labels.join(", ")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          <div className="flex items-center gap-2">
            <code className="rounded bg-secondary px-2 py-1 text-sm">
              {result.data.ticket_code}
            </code>
            <Button
              size="sm"
              variant="ghost"
              aria-label="Copy ticket code"
              onClick={() => void navigator.clipboard.writeText(result.data.ticket_code)}
            >
              <Copy className="h-3.5 w-3.5" />
            </Button>
            {result.replayed && <Badge variant="secondary">Idempotent replay</Badge>}
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => pay.mutate()}
              disabled={pay.isPending}
            >
              Send the same request again
            </Button>
            <Button size="sm" onClick={onClose}>
              Done
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Timer className="h-5 w-5 text-amber-600" />
          <span aria-live="polite">
            {expired ? "Hold expired" : `Held for ${formatClock(secondsLeft ?? 0)}`}
          </span>
        </CardTitle>
        <CardDescription>Seats {hold.seat_labels.join(", ")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <fieldset className="grid gap-2">
          <legend className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Simulated payment outcome (demo only, nothing is charged)
          </legend>
          {(["SUCCESS", "FAILURE"] as const).map((value) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="outcome"
                checked={outcome === value}
                onChange={() => {
                  setOutcome(value);
                  if (pay.error) {
                    pay.reset();
                    setAttemptKey(crypto.randomUUID());
                  }
                }}
              />
              {value === "SUCCESS" ? "Payment succeeds" : "Payment fails (hold is kept)"}
            </label>
          ))}
        </fieldset>
        {pay.error && <ErrorNote error={pay.error} />}
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => pay.mutate()} disabled={pay.isPending || expired}>
            {pay.isPending ? "Processing…" : "Pay & confirm"}
          </Button>
          {pay.error && (
            <Button variant="secondary" onClick={retryWithNewKey}>
              New attempt
            </Button>
          )}
          {expired ? (
            <Button variant="outline" onClick={onClose}>
              Dismiss
            </Button>
          ) : (
            <Button variant="outline" onClick={() => release.mutate()} disabled={release.isPending}>
              Cancel hold
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
