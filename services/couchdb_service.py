"""
Service for interacting with CouchDB.
Handles persistence for alerts and other trading data.
"""
import aiohttp
from config import COUCHDB_URL, COUCHDB_DB_NAME, COUCHDB_BIAS_DB

class CouchDBService:
    def __init__(self):
        self._session = None

    @property
    def _db_url(self):
        return f"{COUCHDB_URL}/{COUCHDB_DB_NAME}"

    @property
    def _bias_url(self):
        return f"{COUCHDB_URL}/{COUCHDB_BIAS_DB}"

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_connection(self):
        """Check if CouchDB is reachable."""
        session = await self._get_session()
        try:
            async with session.get(COUCHDB_URL, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    version = data.get("version", "unknown")
                    print(f"✅ CouchDB connected! | Version: {version} | Host: {COUCHDB_URL.split('@')[-1]}")
                    return True
                else:
                    print(f"❌ CouchDB returned status {resp.status} at {COUCHDB_URL}")
        except Exception as e:
            print(f"❌ CouchDB connection failed: {e}")
        return False

    async def save_alert(self, payload: dict):
        """Save a trading alert to the trading_alerts database."""
        return await self._save_document(self._db_url, payload, COUCHDB_DB_NAME)

    async def save_bias(self, payload: dict):
        """Save the daily bias state to the trading_bias database."""
        # We use the date as the ID to make it easy to find/update
        date_id = payload.get("date")
        if date_id:
            # Check if exists to get _rev for update
            existing = await self.get_bias_by_date(date_id)
            if existing:
                payload["_rev"] = existing["_rev"]
            
            url = f"{self._bias_url}/{date_id}"
            session = await self._get_session()
            try:
                async with session.put(url, json=payload) as resp:
                    if resp.status in [201, 202]:
                        return True
            except Exception as e:
                print(f"❌ CouchDB bias save error: {e}")
        
        return await self._save_document(self._bias_url, payload, COUCHDB_BIAS_DB)

    async def update_narrative_confirmation(self, date_str: str, asset: str, confirmed: bool) -> bool:
        """Update the narrative confirmation state for a specific asset on a given date."""
        existing = await self.get_bias_by_date(date_str)
        if not existing:
            return False
            
        if "narrative_confirmed" not in existing:
            existing["narrative_confirmed"] = {}
            
        existing["narrative_confirmed"][asset] = confirmed
        
        # update document
        url = f"{self._bias_url}/{date_str}"
        session = await self._get_session()
        try:
            async with session.put(url, json=existing) as resp:
                return resp.status in [201, 202]
        except Exception as e:
            print(f"❌ CouchDB narrative update error: {e}")
        return False

    async def get_bias_by_date(self, date_str: str):
        """Fetch bias for a specific date (YYYY-MM-DD)."""
        session = await self._get_session()
        url = f"{self._bias_url}/{date_str}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def _save_document(self, db_url: str, payload: dict, db_name: str):
        """Internal helper to save a document to a specific database."""
        session = await self._get_session()
        try:
            async with session.post(db_url, json=payload) as resp:
                if resp.status == 201:
                    return True
                elif resp.status == 404:
                    if await self._create_database(db_url, db_name):
                        async with session.post(db_url, json=payload) as retry_resp:
                            return retry_resp.status == 201
        except Exception as e:
            print(f"❌ CouchDB error ({db_name}): {e}")
        return False

    async def _create_database(self, db_url: str, db_name: str):
        """Internal helper to create a database if it doesn't exist."""
        print(f"⚠️ CouchDB database '{db_name}' not found. Attempting to create...")
        session = await self._get_session()
        try:
            async with session.put(db_url) as resp:
                return resp.status == 201
        except Exception:
            return False

# Singleton instance
couchdb_service = CouchDBService()
