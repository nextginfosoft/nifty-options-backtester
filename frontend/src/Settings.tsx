import AlertSettings from "./AlertSettings";
import Broker from "./Broker";

/** Everything you set up once: Telegram alerts and the ledger backup, then the Zerodha connection and credentials. */
export default function Settings() {
  return (
    <>
      <h2 className="section-title">Alerts and backup</h2>
      <AlertSettings />
      <h2 className="section-title">Zerodha connection</h2>
      <Broker />
    </>
  );
}
