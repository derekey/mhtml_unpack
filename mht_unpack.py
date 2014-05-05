#!/usr/bin/env python3
"""
Tries fairly hard to repack an MHTML message into a single HTML document using data: URIs.

This passes some, but not all, test cases from http://people.dsv.su.se/~jpalme/mimetest/MHTML-test-messages.html

This does detect and break cycles. It doesn't attempt to hit the network. It may generate data: URIs that are too
large for a browser, or even crash while running.

License: it's not licensed for use, and is protected by copyright. I plan to license it under the same terms as Python, just need to find the proper boilerplate.

Copyright 2013 Ben Samuel
"""

import email as em
import sys
import base64 as b64
import urllib.parse as up
import bs4  # pip install beautifulsoup4
magic_obj = None
try: # pip install filemagic
    import magic
    magic_obj = magic.Magic(flags=magic.MAGIC_NO_CHECK_TAR | magic.MAGIC_NO_CHECK_ELF | magic.MAGIC_MIME_TYPE)
except ImportError:
    pass

import hashlib as hl
import os.path as op
import mimetypes as mt

common_types = {
    'text/html': '.html',
    'text/plain': '.txt',
    'application/octet-stream': '.data',
    'image/jpeg': '.jpg'
}

def find_extension(mime_type):
    """
    Determine an extension for a given mime type.
    """
    mime_type = mime_type.lower()
    try:
        ext = common_types[mime_type]
    except KeyError:
        exts = sorted(mt.guess_all_extensions(mime_type))
        exts.append("")
        ext = exts[0]
        common_types[mime_type] = ext
        print("  {0} -> '{1}'".format(mime_type, ext))
    return ext

class PartHelper:
    def __init__(self, part):
        self.part = part
        mime = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not mime or "octet-stream" in mime:
            _mime = None
            if magic_obj:
                try:
                    _mime = magic_obj.id_buffer(payload)
                except:
                    pass
            if _mime:
                mime = _mime
        if not mime:
            mime = ""
        self.content_type = mime
        self.payload = payload
        self.extension = find_extension(mime)
        digest = hl.sha256(payload).digest()
        self.digest = b64.urlsafe_b64encode(digest).decode("ascii")

class InlineData:
    """
    A mixin to represent objects using inline data URIs.
    """
    def data(binary, content_type):
        """
        Creates a data URI
        :param binary:  binary data
        :param content_type: a mime type, e.g. foo/bar
        :return: the data uri
        """
        return "data:{0};base64,{1}".format(
            content_type, b64.encodebytes(binary).decode()
            .replace("\n", ""))

    def render_data(self, part, seen):
        """
        Given a part, and a set of seen parts, render the data as a data: URI.
        :param part: the message part.
        :param seen: a set of seen parts.
        :return: a URI representing the data.
        """
        if part is None:
            return None
        helper = PartHelper(part)
        if helper.digest in seen:
            return None
        binary, content_type = self.render(helper, seen | {ph})
        return self.data(binary, content_type)

class DataDirectory:
    """
    A mixin to represent objects using a folder of data files.
    """
    def render_data(self, part, seen):
        """
        Given a part, and a set of seen parts, render the data as a relative URI.
        :param part: the message part.
        :param seen: a set of seen parts
        :return: a URI representing the data.
        """
        if part is None:
            return None
        helper = PartHelper(part)
        path = "blob={0}{1}".format(helper.digest, helper.extension)
        if helper.digest in seen:
            return path
        if not op.exists(path):
            binary, content_type = self.render(helper, seen | { helper.digest })
            with open(path, "wb") as fh:
                fh.write(binary)
        return path

class Mapped:
    def __init__(self, mess, **kw):
        """
        Walks a multipart message and builds indexes into the parts using the content-Id and content-location headers.

        Also respects Content-Base, but apparently that's been dropped from the standard.
        :param mess: A message part generated by the standard email package.
        """
        self.by_loc = {}
        self.by_id = {}

        self.starts = set()
        for part in mess.walk():
            start = part.get_param('start', None)
            if start is not None:
                self.starts.add(start)
            base = part.get('Content-Base', "")
            loc = part.get('Content-Location', None)
            if loc is not None:
                self.by_loc[up.urljoin(base, loc)] = part
            cid = part.get('Content-ID', None)
            if cid is not None:
                self.by_id[cid] = self.by_id[cid.strip("<>")] = part

        super().__init__(**kw)

    refs = {
        'a': ['href'],
        'applet': ['codebase'],
        'area': ['href'],
        'audio': ['src'],
        'blockquote': ['cite'],
        'body': ['background'],
        'button': ['formaction'],
        'command': ['icon'],
        'del': ['cite'],
        'embed': ['src'],
        'form': ['action'],
        'frame': ['longdesc', 'src'],
        'head': ['profile'],
        'html': ['manifest'],
        'iframe': ['longdesc', 'src'],
        'img': ['longdesc', 'src', 'usemap'],
        'input': ['formaction', 'src', 'usemap'],
        'ins': ['cite'],
        'link': ['href'],
        'object': ['classid', 'codebase', 'data', 'usemap'],
        'q': ['cite'],
        'script': ['src'],
        'source': ['src'],
        'track': ['src'],
        'video': ['poster', 'src']
    }

    def render(self, helper, seen=frozenset()):
        """
        Renders a message part.
        :param helper: a helper that holds a part of a multipart mime message, or a message part
        :param seen: a set used for cycle detection
        :return: a 2-tup of (binary, mimetype), where mimetype is e.g. "text/html"
        """
        if not isinstance(helper, PartHelper):
            helper = PartHelper(helper)
        data = helper.payload
        content_type = helper.content_type
        part = helper.part
        if content_type == "text/html":
            doc = bs4.BeautifulSoup(data)
            loc = part.get('Content-Location', "").strip()
            base = [up.urljoin(loc, base)
                    for base
                    in doc('base', limit=1) + [part.get('Content-Base', "")]][0]
            for tag in doc.descendants:
                if not isinstance(tag, bs4.Tag):
                    continue
                for attr in Mapped.refs.get(tag.name, ()):
                    href = tag.get(attr, "").strip()
                    if not href:
                        continue
                    href_split = up.urlsplit(href)
                    if href_split.scheme == 'cid':
                        mref = self.by_id.get(href_split.path, None)
                    else:
                        mref = self.by_loc.get(up.urljoin(base, href), None)
                    print("{0}.{1}={2}; {3}".format(tag.name, attr, href, mref is not None))
                    href = self.render_data(mref, seen)
                    if href is not None:
                        tag[attr] = href
            return doc.encode(), 'text/html;charset=utf8'
        if isinstance(data, str):
            return data.encode('utf-8'), "{0};charset=utf8".format(content_type)
        return data, content_type

class MappedInline(Mapped, InlineData):
    pass

class MappedRelative(Mapped, DataDirectory):
    pass

if __name__ == '__main__':
    if "file" in sys.argv[0]:
        con = MappedRelative
    else:
        con = MappedInline
    for path in sys.argv[1:]:
        with open(path, "rb") as fp:
            mess = em.message_from_binary_file(fp)
        mapper = con(mess)
        root = None
        for start in mapper.starts:
            root = mapper.by_id.get(start, None)
            if root is not None:
                break
        if root is None:
            for part in mess.walk():
                if not part.is_multipart():
                    root = part
                    break
        if root is None:
            print(path, ": Can't find root node", file=sys.stderr)
            continue
        binary, mime = mapper.render(root)
        new_path = op.split_ext(path)[0] + ".conv.html"
        with open(new_path, "wb") as fp:
            fp.write(binary)
