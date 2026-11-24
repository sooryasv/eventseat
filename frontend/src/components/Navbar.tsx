import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Armchair, LogOut } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { ErrorNote } from "@/components/ui/state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/sonner";
import { api } from "@/lib/api";
import { queryKeys, useDemoTools, useMe } from "@/lib/hooks";
import type { Role, User } from "@/lib/types";

const PERSONAS: { role: Role; label: string }[] = [
  { role: "ATTENDEE", label: "Attendee" },
  { role: "ORGANIZER", label: "Organizer" },
  { role: "SUPER_ADMIN", label: "Admin" },
];

function useSignedIn() {
  const queryClient = useQueryClient();
  return (user: User) => {
    queryClient.clear();
    queryClient.setQueryData(queryKeys.me, user);
    toast.success(`Signed in as ${user.full_name}`);
  };
}

const EMPTY_FORM = {
  email: "",
  password: "",
  full_name: "",
  role: "ATTENDEE" as "ATTENDEE" | "ORGANIZER",
};

function AuthDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const signedIn = useSignedIn();
  const [form, setForm] = useState(EMPTY_FORM);
  const onSuccess = (user: User) => {
    setForm(EMPTY_FORM);
    signedIn(user);
    onOpenChange(false);
  };
  const login = useMutation({ mutationFn: () => api.login(form.email, form.password), onSuccess });
  const register = useMutation({
    mutationFn: () => api.register(form.email, form.password, form.full_name, form.role),
    onSuccess,
  });
  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setForm(EMPTY_FORM);
      login.reset();
      register.reset();
    }
    onOpenChange(nextOpen);
  };
  const set = (field: keyof typeof form) => (value: string) =>
    setForm((current) => ({ ...current, [field]: value }));

  const submit = (mutation: typeof login | typeof register) => (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Welcome to EventSeat</DialogTitle>
          <DialogDescription>Sign in, or create an account to hold seats.</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="login">
          <TabsList>
            <TabsTrigger value="login">Sign in</TabsTrigger>
            <TabsTrigger value="register">Create account</TabsTrigger>
          </TabsList>
          <TabsContent value="login">
            <form className="grid gap-3" onSubmit={submit(login)}>
              <Label htmlFor="login-email">Email</Label>
              <Input
                id="login-email"
                type="email"
                required
                value={form.email}
                onChange={(e) => set("email")(e.target.value)}
              />
              <Label htmlFor="login-password">Password</Label>
              <Input
                id="login-password"
                type="password"
                required
                value={form.password}
                onChange={(e) => set("password")(e.target.value)}
              />
              {login.error && <ErrorNote error={login.error} />}
              <Button type="submit" disabled={login.isPending}>
                {login.isPending ? "Signing in…" : "Sign in"}
              </Button>
            </form>
          </TabsContent>
          <TabsContent value="register">
            <form className="grid gap-3" onSubmit={submit(register)}>
              <Label htmlFor="reg-name">Full name</Label>
              <Input
                id="reg-name"
                required
                maxLength={120}
                value={form.full_name}
                onChange={(e) => set("full_name")(e.target.value)}
              />
              <Label htmlFor="reg-email">Email</Label>
              <Input
                id="reg-email"
                type="email"
                required
                value={form.email}
                onChange={(e) => set("email")(e.target.value)}
              />
              <Label htmlFor="reg-password">Password (8–128 characters)</Label>
              <Input
                id="reg-password"
                type="password"
                required
                minLength={8}
                maxLength={128}
                value={form.password}
                onChange={(e) => set("password")(e.target.value)}
              />
              <Label htmlFor="reg-role">I want to</Label>
              <Select id="reg-role" value={form.role} onChange={(e) => set("role")(e.target.value)}>
                <option value="ATTENDEE">Book seats (attendee)</option>
                <option value="ORGANIZER">Host events (needs admin verification)</option>
              </Select>
              {register.error && <ErrorNote error={register.error} />}
              <Button type="submit" disabled={register.isPending}>
                {register.isPending ? "Creating…" : "Create account"}
              </Button>
            </form>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

export function Navbar() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const signedIn = useSignedIn();
  const { data: user } = useMe();
  const demoTools = useDemoTools();
  const [authOpen, setAuthOpen] = useState(false);
  const [authKey, setAuthKey] = useState(0);

  const demo = useMutation({
    mutationFn: (persona: Role) => api.demoLogin(persona),
    onSuccess: signedIn,
    onError: (error: Error) => toast.error(error.message),
  });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: () => {
      queryClient.clear();
      queryClient.setQueryData(queryKeys.me, null);
      toast.success("Signed out");
      navigate("/", { replace: true });
    },
  });

  const handleLogout = () => {
    if (window.confirm("Sign out of EventSeat?")) {
      logout.mutate();
    }
  };

  const link = ({ isActive }: { isActive: boolean }) =>
    `text-sm font-medium ${isActive ? "text-primary" : "text-muted-foreground hover:text-foreground"}`;

  return (
    <header className="sticky top-0 z-40 border-b bg-card/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-3">
        <Link to="/" className="flex items-center gap-2 text-lg font-bold">
          <Armchair className="h-5 w-5 text-primary" />
          EventSeat
        </Link>
        <nav className="flex items-center gap-4">
          <NavLink to="/" end className={link}>
            Events
          </NavLink>
          {user && (
            <NavLink to="/my-bookings" className={link}>
              My bookings
            </NavLink>
          )}
          {user && user.role !== "ATTENDEE" && (
            <NavLink to="/admin" className={link}>
              Admin
            </NavLink>
          )}
        </nav>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {user ? (
            <>
              <span className="text-sm">{user.full_name}</span>
              <Badge variant="secondary">{user.role.replace("_", " ")}</Badge>
              <Button
                size="sm"
                variant="outline"
                onClick={handleLogout}
                disabled={logout.isPending}
              >
                <LogOut className="h-3.5 w-3.5" /> Sign out
              </Button>
            </>
          ) : (
            <>
              {demoTools && (
                <>
                  <span className="hidden text-xs text-muted-foreground sm:inline">
                    Demo login:
                  </span>
                  {PERSONAS.map(({ role, label }) => (
                    <Button
                      key={role}
                      size="sm"
                      variant="secondary"
                      disabled={demo.isPending}
                      onClick={() => demo.mutate(role)}
                    >
                      {label}
                    </Button>
                  ))}
                </>
              )}
              <Button
                size="sm"
                onClick={() => {
                  setAuthKey((k) => k + 1);
                  setAuthOpen(true);
                }}
              >
                Sign in
              </Button>
            </>
          )}
        </div>
      </div>
      <AuthDialog key={authKey} open={authOpen} onOpenChange={setAuthOpen} />
    </header>
  );
}
