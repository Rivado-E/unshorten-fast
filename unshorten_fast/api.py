""" Expand URLs from shortening services """

import aiohttp
import argparse
import asyncio
import time
from statistics import mean, stdev
import logging
from urllib.parse import urlsplit
import re

TTL_DNS_CACHE=300  # Time-to-live of DNS cache
MAX_TCP_CONN=200  # Throttle at max these many simultaneous connections
TIMEOUT_TOTAL=10  # Each request times out after these many seconds

LOG_FMT = "%(asctime)s:%(levelname)s:%(message)s"
logging.basicConfig(format=LOG_FMT, level="INFO") 
_STATS = {
    "ignored": 0,
    "timeout": 0,
    "error": 0,
    "cached": 0,
    "cached_retrieved": 0,
    "expanded": 0,
    "elapsed_a": [],
    "elapsed_e": [],
}


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("-m", 
                        "--maxlen", 
                        type=int, 
                        metavar="LEN",
                        help="Ignore domains longer than %(metavar)s")
    parser.add_argument("-d", 
                        "--domains", 
                        dest="domains_path",
                        metavar="PATH",
                        help="Expand if domain is present in CSV file at %(metavar)s")
    parser.add_argument("--domains-noheader", 
                        action="store_false",
                        dest="skip_header",
                        help="CSV file with domains has no header")
    parser.add_argument("--no-cache", 
                        action="store_true", 
                        help="disable cache")
    parser.add_argument("--debug", 
                        action="store_const", 
                        const="DEBUG",
                        dest="log_level")
    parser.set_defaults(log_level="INFO")
    return parser


async def unshortenone(url, session, pattern=None, maxlen=None, 
                       cache=None, timeout=None):
    # If user specified list of domains, check netloc is in it, otherwise set
    # to False (equivalent of saying there is always a match against the empty list)
    if pattern is not None:
        domain = urlsplit(url).netloc
        match = re.search(pattern, domain)
        no_match = (match is None)
    else:
        no_match = False
    # If user specified max URL length, check length, otherwise set to False
    # (equivalent to setting max length to infinity -- any length is OK)
    too_long = (maxlen is not None and len(url) > maxlen)
    # Ignore if either of the two exclusion criteria applies.
    if too_long or no_match:
        _STATS["ignored"] += 1
        return url
    if cache is not None and url in cache:
        _STATS["cached_retrieved"] += 1
        return str(cache[url])
    else:
        try:
            # await asyncio.sleep(0.01)
            req_start = time.time()
            resp = await session.head(url, timeout=timeout, 
                                      ssl=False, allow_redirects=True)
            req_stop = time.time()
            elapsed = req_stop - req_start
            expanded_url = str(resp.url)
            _STATS['elapsed_a'].append(elapsed)
            if url != expanded_url:
                _STATS['expanded'] += 1
                _STATS['elapsed_e'].append(elapsed)
                if cache is not None and url not in cache:
                    # update cache if needed
                    _STATS["cached"] += 1
                    cache[url] = expanded_url
            return expanded_url
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as e:
            req_stop = time.time()
            elapsed = req_stop - req_start
            _STATS['elapsed_a'].append(elapsed)
            _STATS["error"] += 1
            if isinstance(e, asyncio.TimeoutError):
                _STATS["timeout"] += 1
            logging.debug(f"{e.__class__.__name__}: {e}: {url}")
            return url


# Thanks: https://blog.jonlu.ca/posts/async-python-http
async def gather_with_concurrency(n, *tasks):
    semaphore = asyncio.Semaphore(n)
    async def sem_task(task):
        async with semaphore:
            return await task
    return await asyncio.gather(*(sem_task(task) for task in tasks))


async def _unshorten(*urls, cache=None, domains=None, maxlen=None):
    if domains is not None:
        pattern = re.compile(f"({'|'.join(domains)})", re.I)
    else:
        pattern = None
    conn = aiohttp.TCPConnector(ttl_dns_cache=TTL_DNS_CACHE, limit=None)
    u1 = unshortenone
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_TOTAL)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await gather_with_concurrency(MAX_TCP_CONN, 
                                             *(u1(u, session, cache=cache,
                                                  maxlen=maxlen,
                                                  pattern=pattern, 
                                                  timeout=timeout) for u in urls))


def unshorten(*args, **kwargs):
    return asyncio.run(_unshorten(*args, **kwargs))


def _log_elapsed_ms(seq, what):
    if seq:
        elap_av = mean(seq) / 1e3
        elap_sd = stdev(seq) / 1e3
        logging.info(f"{what}: {elap_av:.5f}±{elap_sd:.5f} ms")
    else:
        logging.info(f"{what}: N/A")


def _main(args):
    try:
        logging.basicConfig(level=args.log_level, format=LOG_FMT, force=True)
        logging.info(args)
        if args.domains_path is not None:
            with open(args.domains_path) as f:
                if args.skip_header:
                    f.readline()
                domains = [line.strip(',\n') for line in f]
        else:
            domains = None
        if args.no_cache:
            cache = None
        else:
            cache = {}
        tic = time.time()
        with open(args.input, encoding="utf8") as inputf:
            shorturls = (url.strip(" \n") for url in inputf)
            urls = unshorten(*shorturls, cache=cache, domains=domains, 
                             maxlen=args.maxlen)
        with open(args.output, "w", encoding="utf8") as outf:
            outf.writelines((u + "\n" for u in urls))
        toc = time.time()
        elapsed = toc - tic
        rate = len(urls) / elapsed
        logging.info(f"Processed {len(urls)} urls in {elapsed:.2f}s ({rate:.2f} urls/s))")
    except KeyboardInterrupt:
        import sys
        print(file=sys.stderr)
        logging.info("Interrupted by user.")
    finally:
        _log_elapsed_ms(_STATS['elapsed_a'], "Elapsed (all)")
        _log_elapsed_ms(_STATS['elapsed_e'], "Elapsed (expanded)")
        logging.info(f"Ignored: {_STATS['ignored']:.0f}")
        logging.info(f"Expanded: {_STATS['expanded']:.0f}")
        logging.info(f"Cached: {_STATS['cached']:.0f} ({_STATS['cached_retrieved']:.0f} hits)")
        logging.info(f"Errors: {_STATS['error']:.0f} ({_STATS['timeout']:.0f} timed out)")


def main():
    parser = make_parser()
    args = parser.parse_args()
    _main(args)


if __name__ == "__main__":
    main()
