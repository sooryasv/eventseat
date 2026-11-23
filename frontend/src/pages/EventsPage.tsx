import { CalendarClock, MapPin, Search } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Empty, ErrorNote, Loading } from "@/components/ui/state";
import { formatDateTime, useEvents } from "@/lib/hooks";
import { CATEGORIES, type Category, type EventsParams } from "@/lib/types";

const PAGE_SIZE = 9;

export function EventsPage() {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<Category | "">("");
  const [page, setPage] = useState(1);

  const params: EventsParams = {
    q: search.trim(),
    category: category === "" ? undefined : category,
    page,
    page_size: PAGE_SIZE,
  };
  const { data, isLoading, error } = useEvents(params);
  const lastPage = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="grid gap-5">
      <div>
        <h1 className="text-2xl font-bold">Upcoming events</h1>
        <p className="text-sm text-muted-foreground">
          Pick an event, choose up to 4 seats, hold them for 5 minutes.
        </p>
      </div>
      <div className="flex flex-wrap gap-3">
        <div className="relative min-w-56 flex-1">
          <Search className="absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-9"
            placeholder="Search by title or venue"
            aria-label="Search events"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <Select
          className="w-48"
          aria-label="Filter by category"
          value={category}
          onChange={(e) => {
            setCategory(e.target.value as Category | "");
            setPage(1);
          }}
        >
          <option value="">All categories</option>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c.charAt(0) + c.slice(1).toLowerCase()}
            </option>
          ))}
        </Select>
      </div>

      {isLoading && <Loading label="Loading events…" />}
      {error && <ErrorNote error={error} />}
      {data && data.items.length === 0 && <Empty>No events match your search.</Empty>}
      {data && data.items.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.items.map((event) => (
            <Card key={event.id} className="flex flex-col">
              <CardHeader>
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5">
                    <Badge variant="secondary">{event.category}</Badge>
                    {event.status === "CANCELLED" && <Badge variant="destructive">Cancelled</Badge>}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {event.status === "CANCELLED"
                      ? "Event cancelled"
                      : `${event.available_seats}/${event.total_seats} seats free`}
                  </span>
                </div>
                <CardTitle>{event.title}</CardTitle>
              </CardHeader>
              <CardContent className="mt-auto grid gap-3 text-sm text-muted-foreground">
                <p className="flex items-center gap-2">
                  <MapPin className="h-4 w-4" /> {event.venue}
                </p>
                <p className="flex items-center gap-2">
                  <CalendarClock className="h-4 w-4" /> {formatDateTime(event.starts_at)}
                </p>
                <Link
                  to={`/events/${event.id}`}
                  className={buttonVariants(
                    event.status === "CANCELLED"
                      ? { size: "sm", variant: "outline" }
                      : { size: "sm" },
                  )}
                >
                  {event.status === "CANCELLED" ? "View details" : "View seats"}
                </Link>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {data && lastPage > 1 && (
        <div className="flex items-center justify-center gap-3 text-sm">
          <Button
            size="sm"
            variant="outline"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </Button>
          <span>
            Page {page} of {lastPage}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={page >= lastPage}
            onClick={() => setPage(page + 1)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
