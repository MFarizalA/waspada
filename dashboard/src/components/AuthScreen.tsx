import { useState } from "react";
import {
  AuthError,
  DEMO_EMAIL,
  DEMO_PASSWORD,
  forgotPassword,
  resetPassword,
  useAuth,
} from "@/lib/auth";
import styles from "./AuthScreen.module.css";

type Mode = "login" | "register" | "forgot" | "reset";

interface AuthScreenProps {
  /** Initial mode. Defaults to "login". */
  initialMode?: Mode;
}

/**
 * Auth screen — login / register / forgot-password / reset-password. One
 * component, four modes, same a11y & styling baseline as the rest of the app.
 *
 * - Login shows the demo analyst credentials as a one-click fill hint so judges
 *   don't have to look them up.
 * - Every field is labelled; errors render in role="alert"; the submit button
 *   shows a pending state and is disabled while a request is in flight.
 * - Mode switches are plain buttons (not links) so keyboard focus stays in the
 *   form; each form autofocuses its first field.
 *
 * Identity flows (login/register) go through the auth context; forgot/reset are
 * standalone helpers that return to login on success.
 */
export function AuthScreen({ initialMode = "login" }: AuthScreenProps) {
  const [mode, setMode] = useState<Mode>(initialMode);
  return (
    <div className={styles.wrap}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <img src="favicon.svg" alt="" width="36" height="36" className={styles.brandMark} />
          <div>
            <h1 className={styles.title}>WASPADA · EWS</h1>
            <p className={styles.subtitle}>Early-warning collections</p>
          </div>
        </div>
        {mode === "login" && <LoginForm onSwitch={setMode} />}
        {mode === "register" && <RegisterForm onSwitch={setMode} />}
        {mode === "forgot" && <ForgotForm onSwitch={setMode} />}
        {mode === "reset" && <ResetForm onSwitch={setMode} />}
      </div>
    </div>
  );
}

// --- shared field ----------------------------------------------------------
interface FieldProps {
  id: string;
  label: string;
  type?: "email" | "password" | "text";
  value: string;
  onChange: (v: string) => void;
  autoComplete?: string;
  autoFocus?: boolean;
  required?: boolean;
  minLength?: number;
}

function Field({
  id,
  label,
  type = "text",
  value,
  onChange,
  autoComplete,
  autoFocus,
  required = true,
  minLength,
}: FieldProps) {
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>
        {label}
      </label>
      <input
        id={id}
        type={type}
        className={styles.input}
        value={value}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        required={required}
        minLength={minLength}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

// --- error banner ----------------------------------------------------------
function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <p className={styles.error} role="alert">
      {error}
    </p>
  );
}

// --- shared submit ---------------------------------------------------------
function SubmitButton({ label, pending }: { label: string; pending: boolean }) {
  return (
    <button type="submit" className={styles.submit} disabled={pending}>
      {pending ? "…" : label}
    </button>
  );
}

/** Map an auth error to a user-facing message. Network failures get a hint to
 *  check the backend; AuthError carries the backend's own detail otherwise. */
function friendlyMessage(err: unknown, fallback: string): string {
  if (err instanceof AuthError) return err.message || fallback;
  return "Couldn’t reach the server. Check it’s running on :8080.";
}

// --- LOGIN -----------------------------------------------------------------
function LoginForm({ onSwitch }: { onSwitch: (m: Mode) => void }) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fillDemo = () => {
    setEmail(DEMO_EMAIL);
    setPassword(DEMO_PASSWORD);
    setError(null);
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      await login(email, password);
    } catch (err) {
      setError(
        err instanceof AuthError && err.status === 401
          ? "Invalid email or password."
          : friendlyMessage(err, "Sign-in failed."),
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <form onSubmit={onSubmit} className={styles.form}>
      <h2 className={styles.heading}>Sign in</h2>
      <ErrorBanner error={error} />
      <Field
        id="login-email"
        label="Email"
        type="email"
        value={email}
        onChange={setEmail}
        autoComplete="email"
        autoFocus
      />
      <Field
        id="login-password"
        label="Password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="current-password"
        minLength={8}
      />

      <button
        type="button"
        className={styles.demoHint}
        onClick={fillDemo}
        aria-label="Fill demo analyst credentials"
      >
        <span className={styles.demoLabel}>Demo analyst</span>
        <span className={styles.demoCreds}>
          {DEMO_EMAIL} · {DEMO_PASSWORD}
        </span>
      </button>

      <SubmitButton label="Sign in" pending={pending} />

      <div className={styles.links}>
        <button type="button" className={styles.link} onClick={() => onSwitch("register")}>
          Create an account
        </button>
        <button type="button" className={styles.link} onClick={() => onSwitch("forgot")}>
          Forgot password?
        </button>
      </div>
    </form>
  );
}

// --- REGISTER --------------------------------------------------------------
function RegisterForm({ onSwitch }: { onSwitch: (m: Mode) => void }) {
  const { register } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError("Passwords don’t match.");
      return;
    }
    setPending(true);
    try {
      await register(email, password);
    } catch (err) {
      setError(
        err instanceof AuthError && err.status === 409
          ? "That email is already registered. Try signing in."
          : friendlyMessage(err, "Registration failed."),
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <form onSubmit={onSubmit} className={styles.form}>
      <h2 className={styles.heading}>Create account</h2>
      <ErrorBanner error={error} />
      <Field
        id="reg-email"
        label="Email"
        type="email"
        value={email}
        onChange={setEmail}
        autoComplete="email"
        autoFocus
      />
      <Field
        id="reg-password"
        label="Password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="new-password"
        minLength={8}
      />
      <Field
        id="reg-confirm"
        label="Confirm password"
        type="password"
        value={confirm}
        onChange={setConfirm}
        autoComplete="new-password"
        minLength={8}
      />
      <p className={styles.hint}>Minimum 8 characters.</p>
      <SubmitButton label="Create account" pending={pending} />
      <div className={styles.links}>
        <button type="button" className={styles.link} onClick={() => onSwitch("login")}>
          ← Back to sign in
        </button>
      </div>
    </form>
  );
}

// --- FORGOT ----------------------------------------------------------------
interface ForgotDoneState {
  email: string;
  resetToken?: string;
}

function ForgotForm({ onSwitch }: { onSwitch: (m: Mode) => void }) {
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<ForgotDoneState | null>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setPending(true);
    try {
      const res = await forgotPassword(email);
      setDone({ email, resetToken: res.resetToken });
    } catch (err) {
      setError(friendlyMessage(err, "Couldn’t send reset token."));
    } finally {
      setPending(false);
    }
  };

  if (done) {
    return (
      <div className={styles.form}>
        <h2 className={styles.heading}>Check your email</h2>
        <p className={styles.confirmText}>
          If <strong>{done.email}</strong> is registered, a reset token has been
          issued. In production it would land in your inbox; for this demo the
          token is shown below.
        </p>
        {done.resetToken && (
          <div className={styles.tokenBox}>
            <p className={styles.tokenLabel}>Reset token (demo delivery)</p>
            <code className={styles.tokenValue}>{done.resetToken}</code>
          </div>
        )}
        <div className={styles.links}>
          <button type="button" className={styles.link} onClick={() => onSwitch("reset")}>
            I have a token →
          </button>
          <button type="button" className={styles.link} onClick={() => onSwitch("login")}>
            Back to sign in
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className={styles.form}>
      <h2 className={styles.heading}>Forgot password</h2>
      <ErrorBanner error={error} />
      <Field
        id="forgot-email"
        label="Email"
        type="email"
        value={email}
        onChange={setEmail}
        autoComplete="email"
        autoFocus
      />
      <SubmitButton label="Send reset token" pending={pending} />
      <div className={styles.links}>
        <button type="button" className={styles.link} onClick={() => onSwitch("login")}>
          ← Back to sign in
        </button>
      </div>
    </form>
  );
}

// --- RESET -----------------------------------------------------------------
function ResetForm({ onSwitch }: { onSwitch: (m: Mode) => void }) {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError("Passwords don’t match.");
      return;
    }
    setPending(true);
    try {
      await resetPassword(token, password);
      setDone(true);
    } catch (err) {
      setError(
        err instanceof AuthError && err.status === 401
          ? "Invalid or expired reset token."
          : friendlyMessage(err, "Reset failed."),
      );
    } finally {
      setPending(false);
    }
  };

  if (done) {
    return (
      <div className={styles.form}>
        <h2 className={styles.heading}>Password updated</h2>
        <p className={styles.confirmText}>Sign in with your new password.</p>
        <div className={styles.links}>
          <button type="button" className={styles.link} onClick={() => onSwitch("login")}>
            → Back to sign in
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className={styles.form}>
      <h2 className={styles.heading}>Reset password</h2>
      <ErrorBanner error={error} />
      <Field
        id="reset-token"
        label="Reset token"
        type="text"
        value={token}
        onChange={setToken}
        autoFocus
      />
      <Field
        id="reset-password"
        label="New password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="new-password"
        minLength={8}
      />
      <Field
        id="reset-confirm"
        label="Confirm new password"
        type="password"
        value={confirm}
        onChange={setConfirm}
        autoComplete="new-password"
        minLength={8}
      />
      <SubmitButton label="Update password" pending={pending} />
      <div className={styles.links}>
        <button type="button" className={styles.link} onClick={() => onSwitch("login")}>
          ← Back to sign in
        </button>
      </div>
    </form>
  );
}
