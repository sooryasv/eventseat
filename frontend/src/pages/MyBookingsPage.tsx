import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, ErrorNote, Loading } from "@/components/ui/state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/sonner";
import { api } from "@/lib/api";
import { formatClock, formatDateTime, queryKeys, useMe, useMyBookings } from "@/lib/hooks";
import type { Booking, BookingStatus } from "@/lib/types";

const STATUS_VARIANT: Record<BookingStatus, "default" | "secondary" | "destructive"> = {
  HELD: "secondary",
  CONFIRMED: "default",
  CANCELLED: "destructive",
  EXPIRED: "destructive",
};

function BookingsTable({ rows, cancellable }: { rows: Booking[]; cancellable: boolean }) {
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: (bookingId: string) => api.cancelBooking(bookingId),
    onSuccess: () => {
      toast.success("Booking cancelled");
      void queryClient.invalidateQueries({ queryKey: queryKeys.bookings });
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["seat-map"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  if (rows.length === 0) return <Empty>Nothing here yet.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Event</TableHead>
          <TableHead>Seats</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Details</TableHead>
          {cancellable && <TableHead />}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.id}>
            <TableCell>
              <Link to={`/events/${row.event_id}`} className="font-medium hover:underline">
                {row.event_title}
              </Link>
              <div className="text-xs text-muted-foreground">
                {formatDateTime(row.event_starts_at)}
              </div>
            </TableCell>
            <TableCell>{row.seat_labels.join(", ")}</TableCell>
            <TableCell>
              <Badge variant={STATUS_VARIANT[row.status]}>{row.status}</Badge>
            </TableCell>
            <TableCell className="text-xs">
              <div className="flex flex-wrap items-center gap-2">
                {row.ticket_code && (
                  <code className="rounded bg-secondary px-1.5 py-0.5">{row.ticket_code}</code>
                )}
                {row.remaining_seconds !== null && (
                  <span>{formatClock(row.remaining_seconds)} left (open event to pay)</span>
                )}
                {row.cancelled_at && <span>Cancelled {formatDateTime(row.cancelled_at)}</span>}
              </div>
            </TableCell>
            {cancellable && (
              <TableCell>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={cancel.isPending}
                  onClick={() => cancel.mutate(row.id)}
                >
                  Cancel
                </Button>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function MyBookingsPage() {
  const { data: user, isLoading: userLoading } = useMe();
  const { data, isLoading, error } = useMyBookings(Boolean(user));

  if (userLoading) return <Loading />;
  if (!user) return <Empty>Sign in to see your bookings.</Empty>;

  return (
    <Card>
      <CardHeader>
        <CardTitle>My bookings</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <Loading label="Loading bookings…" />}
        {error && <ErrorNote error={error} />}
        {data && (
          <Tabs defaultValue="tickets">
            <TabsList>
              <TabsTrigger value="tickets">Tickets ({data.tickets.length})</TabsTrigger>
              <TabsTrigger value="holds">Active holds ({data.holds.length})</TabsTrigger>
              <TabsTrigger value="history">History ({data.history.length})</TabsTrigger>
            </TabsList>
            <TabsContent value="tickets">
              <BookingsTable rows={data.tickets} cancellable />
            </TabsContent>
            <TabsContent value="holds">
              <BookingsTable rows={data.holds} cancellable />
            </TabsContent>
            <TabsContent value="history">
              <BookingsTable rows={data.history} cancellable={false} />
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  );
}
