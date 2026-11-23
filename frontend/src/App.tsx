import { Link, Route, Routes } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { buttonVariants } from "@/components/ui/button";
import { Empty, Loading } from "@/components/ui/state";
import { useMe } from "@/lib/hooks";
import { AdminPage } from "@/pages/AdminPage";
import { EventDetailPage } from "@/pages/EventDetailPage";
import { EventsPage } from "@/pages/EventsPage";
import { MyBookingsPage } from "@/pages/MyBookingsPage";

function RequireStaff() {
  const { data: user, isLoading } = useMe();
  if (isLoading) return <Loading />;
  if (!user || user.role === "ATTENDEE") {
    return <Empty>Sign in as an organizer or admin to open this page.</Empty>;
  }
  return <AdminPage user={user} />;
}

function NotFound() {
  return (
    <Empty>
      <p className="mb-3">That page does not exist.</p>
      <Link to="/" className={buttonVariants({ variant: "outline" })}>
        Back to events
      </Link>
    </Empty>
  );
}

export function App() {
  return (
    <div className="min-h-screen">
      <Navbar />
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/" element={<EventsPage />} />
          <Route path="/events/:eventId" element={<EventDetailPage />} />
          <Route path="/my-bookings" element={<MyBookingsPage />} />
          <Route path="/admin" element={<RequireStaff />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
