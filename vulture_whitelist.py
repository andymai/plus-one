# Names vulture can't see used. Run through pyproject.toml's [tool.vulture], never imported.
from plus1.cli import gather
from plus1.store import Store
from plus1.web import app, words

# http.server and socketserver read these by name.
app.Handler.server_version
app.Handler.protocol_version
app.Handler.log_message
app.Handler.address_string
app.Handler.do_GET
app.Handler.do_POST
app.Handler.close_connection
app.TcpServer.daemon_threads
app.UnixServer.daemon_threads
# sqlite3 reads it by name.
Store.row_factory
# Templates read these as words.NAME.
words.PROPOSAL_TONE
words.STAGE
words.ACTION
words.factcheck_note
words.MODEL_ROLES
# gather's loop extends each list in place with +=.
gather.into
