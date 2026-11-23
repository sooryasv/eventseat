import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
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
import { formatDateTime, useAnalytics, useEvents, useOrganizers } from "@/lib/hooks";
import { CATEGORIES, type Category, type EventCreate, type User } from "@/lib/types";

/** `datetime-local` value (no zone) for a moment `hours` from now, in the browser's zone. */
function localInput(hours: number): string {
  const moment = new Date(Date.now() + hours * 3_600_000);
  moment.setMinutes(moment.getMinutes() - moment.getTimezoneOffset());
  return moment.toISOString().slice(0, 16);
}

function CreateEventForm() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    title: "",
    category: "CONFERENCE" as Category,
    venue: "",
    starts_at: localInput(72),
    booking_closes_at: localInput(48),
    rows: 5,
    cols: 8,
  });
  const create = useMutation({
    mutationFn: (payload: EventCreate) => api.createEvent(payload),
    onSuccess: (event) => {
      toast.success(`Created "${event.title}"`);
      void queryClient.invalidateQueries({ queryKey: ["events"] });
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate({
      ...form,
      starts_at: new Date(form.starts_at).toISOString(),
      booking_closes_at: new Date(form.booking_closes_at).toISOString(),
    });
  };
  const text = (field: "title" | "venue" | "starts_at" | "booking_closes_at") => ({
    value: form[field],
    onChange: (e: { target: { value: string } }) => setForm({ ...form, [field]: e.target.value }),
  });
  const num = (field: "rows" | "cols") => ({
    type: "number",
    min: 1,
    max: field === "rows" ? 10 : 12,
    required: true,
    value: form[field],
    onChange: (e: { target: { value: string } }) =>
      setForm({ ...form, [field]: Number(e.target.value) }),
  });

  return (
    <form className="grid max-w-xl gap-3" onSubmit={submit}>
      <Label htmlFor="ev-title">Title</Label>
      <Input id="ev-title" required maxLength={160} {...text("title")} />
      <Label htmlFor="ev-category">Category</Label>
      <Select
        id="ev-category"
        value={form.category}
        onChange={(e) => setForm({ ...form, category: e.target.value as Category })}
      >
        {CATEGORIES.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </Select>
      <Label htmlFor="ev-venue">Venue</Label>
      <Input id="ev-venue" required maxLength={200} {...text("venue")} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label htmlFor="ev-start">Starts</Label>
          <Input id="ev-start" type="datetime-local" required {...text("starts_at")} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="ev-close">Booking closes</Label>
          <Input id="ev-close" type="datetime-local" required {...text("booking_closes_at")} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="ev-rows">Rows (A, B, …)</Label>
          <Input id="ev-rows" {...num("rows")} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="ev-cols">Seats per row</Label>
          <Input id="ev-cols" {...num("cols")} />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">Creates {form.rows * form.cols} seats.</p>
      {create.error && <ErrorNote error={create.error} />}
      <Button type="submit" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create event"}
      </Button>
    </form>
  );
}

function MyEvents({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const events = useEvents({ page_size: 50 });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const analytics = useAnalytics(selectedId);
  const managed = (events.data?.items ?? []).filter(
    (event) => user.role === "SUPER_ADMIN" || event.organizer_id === user.id,
  );

  const cancel = useMutation({
    mutationFn: (eventId: string) => api.cancelEvent(eventId),
    onSuccess: () => {
      toast.success("Event cancelled; holds and bookings released");
      void queryClient.invalidateQueries({ queryKey: ["events"] });
      void queryClient.invalidateQueries({ queryKey: ["analytics"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  if (events.isLoading) return <Loading label="Loading events…" />;
  if (events.error) return <ErrorNote error={events.error} />;
  if (managed.length === 0) return <Empty>You have no upcoming events yet.</Empty>;

  const stats = analytics.data;
  return (
    <div className="grid gap-5">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Event</TableHead>
            <TableHead>Starts</TableHead>
            <TableHead>Status</TableHead>
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {managed.map((event) => (
            <TableRow key={event.id}>
              <TableCell className="font-medium">{event.title}</TableCell>
              <TableCell>{formatDateTime(event.starts_at)}</TableCell>
              <TableCell>
                <Badge variant={event.status === "CANCELLED" ? "destructive" : "secondary"}>
                  {event.status}
                </Badge>
              </TableCell>
              <TableCell className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => setSelectedId(event.id)}>
                  Analytics
                </Button>
                {event.status === "PUBLISHED" && (
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={cancel.isPending}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Cancel "${event.title}"? This releases every hold and booking.`,
                        )
                      ) {
                        cancel.mutate(event.id);
                      }
                    }}
                  >
                    Cancel event
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {selectedId && analytics.isLoading && <Loading label="Loading analytics…" />}
      {analytics.error && <ErrorNote error={analytics.error} />}
      {stats && (
        <Card>
          <CardHeader>
            <CardTitle>Seat utilisation: {stats.utilization_pct}%</CardTitle>
            <CardDescription>
              {stats.confirmed} confirmed, {stats.held} held, {stats.available} available of{" "}
              {stats.total_seats}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {stats.roster.length === 0 ? (
              <Empty>No confirmed attendees yet.</Empty>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Attendee</TableHead>
                    <TableHead>Seats</TableHead>
                    <TableHead>Confirmed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.roster.map((entry) => (
                    <TableRow key={entry.booking_id}>
                      <TableCell>
                        {entry.full_name}
                        <div className="text-xs text-muted-foreground">{entry.email}</div>
                      </TableCell>
                      <TableCell>{entry.seat_labels.join(", ")}</TableCell>
                      <TableCell>{formatDateTime(entry.confirmed_at)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Organizers() {
  const queryClient = useQueryClient();
  const organizers = useOrganizers(true);
  const verify = useMutation({
    mutationFn: ({ id, value }: { id: string; value: boolean }) => api.verifyOrganizer(id, value),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["organizers"] }),
    onError: (error: Error) => toast.error(error.message),
  });

  if (organizers.isLoading) return <Loading label="Loading organizers…" />;
  if (organizers.error) return <ErrorNote error={organizers.error} />;
  if (!organizers.data || organizers.data.length === 0)
    return <Empty>No organizers have registered.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Organizer</TableHead>
          <TableHead>Verified</TableHead>
          <TableHead />
        </TableRow>
      </TableHeader>
      <TableBody>
        {organizers.data.map((organizer) => (
          <TableRow key={organizer.id}>
            <TableCell>
              {organizer.full_name}
              <div className="text-xs text-muted-foreground">{organizer.email}</div>
            </TableCell>
            <TableCell>
              <Badge variant={organizer.is_verified_organizer ? "default" : "secondary"}>
                {organizer.is_verified_organizer ? "Verified" : "Pending"}
              </Badge>
            </TableCell>
            <TableCell>
              <Button
                size="sm"
                variant="outline"
                disabled={verify.isPending}
                onClick={() =>
                  verify.mutate({ id: organizer.id, value: !organizer.is_verified_organizer })
                }
              >
                {organizer.is_verified_organizer ? "Revoke" : "Verify"}
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function AdminPage({ user }: { user: User }) {
  const isAdmin = user.role === "SUPER_ADMIN";
  const canCreate = isAdmin || user.is_verified_organizer;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{isAdmin ? "Admin console" : "Organizer console"}</CardTitle>
        {!canCreate && (
          <CardDescription>
            Your organizer account is awaiting verification by an admin before you can create
            events.
          </CardDescription>
        )}
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="events">
          <TabsList>
            <TabsTrigger value="events">My events</TabsTrigger>
            {canCreate && <TabsTrigger value="create">Create event</TabsTrigger>}
            {isAdmin && <TabsTrigger value="organizers">Organizers</TabsTrigger>}
          </TabsList>
          <TabsContent value="events">
            <MyEvents user={user} />
          </TabsContent>
          {canCreate && (
            <TabsContent value="create">
              <CreateEventForm />
            </TabsContent>
          )}
          {isAdmin && (
            <TabsContent value="organizers">
              <Organizers />
            </TabsContent>
          )}
        </Tabs>
      </CardContent>
    </Card>
  );
}
