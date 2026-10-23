import asyncio
import sys

# Proactor UDP shutdown can leave aioice's connection_lost future unresolved.
# The deployed Linux image uses the selector loop; use it on Windows as well.
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
