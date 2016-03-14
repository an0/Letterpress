#! /usr/bin/env python3
# -*- coding: utf-8 -*-

__version_info__ = (0, 0, 2)
__version__ = '.'.join(map(str, __version_info__))
__author__ = "Ling Wang"

import sys

argparse = None

if sys.version_info.minor < 2:
    import optparse
else:
    import argparse

import re
import markdown2
import logging
import logging.handlers
import codecs
import datetime
import os.path
import urllib.parse
import shutil
import itertools
from functools import total_ordering
import pyinotify
import email.utils
import html

#--- globals ---
logger = logging.getLogger('Letterpress')


_meta_data_re = re.compile(r"""(?:\s*\n)*((?:\w+:.*\n)+)(?:\s*\n)+""", re.U)


def extract_meta_data(text):
    meta_data = {}

    m = _meta_data_re.match(text)
    if not m:
        logger.error('No meta data')
        return meta_data, text

    lines = m.group(1).splitlines()
    for line in lines:
        k, v = line.split(':', 1)
        v = v.strip()
        if v:
            meta_data[k] = v

    return meta_data, text[m.end():]


_template_re = re.compile(r'{{([^{}]+)}}')


def format(template, **kwargs):
    # Add common replacements to all templates.
    kwargs['common_head'] = common_head
    kwargs['common_header'] = common_header
    return _template_re.sub(lambda m: kwargs[m.group(1)], template)

pygments_options = {'cssclass': 'code', 'classprefix': 'code-'}


@total_ordering
class Post(object):

    def __new__(cls, file_path, base_url, templates_dir, date_format, math_delimiter):
        file_name = os.path.basename(file_path)
        logger.debug('Post: %s', file_name)
        text = ""
        with codecs.open(file_path, 'r', 'utf-8') as f:
            text = f.read()
        meta_data, rest_text = extract_meta_data(text)
        logger.debug('Meta: %s', meta_data)
        if not meta_data.get('title'):
            logger.error('Missing title')
            return None
        if not meta_data.get('date'):
            logger.error('Missing date')
            return None
        self = super(Post, cls).__new__(cls)
        self.meta_data = meta_data
        self.rest_text = rest_text
        return self

    def __init__(self, file_path, base_url, templates_dir, date_format, math_delimiter):
        meta_data = self.meta_data
        del self.meta_data
        rest_text = self.rest_text
        del self.rest_text
        self.file_path = file_path
        self.title = html.escape(meta_data['title'])
        self.date = datetime.datetime.strptime(meta_data['date'], date_format)
        self.pretty_date = self.date.strftime('%B %d, %Y')
        self.excerpt = meta_data.get('excerpt')
        if not self.excerpt:
            if (len(rest_text) > 140):
                self.excerpt = rest_text[:140] + '…'
            else:
                self.excerpt = rest_text
        self.excerpt = html.escape(self.excerpt)
        self.tags = []
        is_math = False
        for tag_name in meta_data.get('tags', '').split(','):
            tag_name = tag_name.strip()
            if tag_name:
                self.tags.append(tag_name)
                if tag_name.lower() == 'math':
                    is_math = True
        self.lang = meta_data.get('lang')
        if self.lang == 'Chinese' or self.lang == '中文':
            template_file_name = 'post_zh.html'
        else:
            template_file_name = 'post.html'
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        self.path = '{year:04}/{month:02}/{base_name}.html'.format(
            year=self.date.year, month=self.date.month, base_name=base_name.lower().replace(' ', '-'))
        self.permalink = os.path.join(base_url, self.path)
        with codecs.open(os.path.join(templates_dir, template_file_name), 'r', 'utf-8') as f:
            template = f.read()
        self.content = markdown2.markdown(rest_text, extras={
                                          'code-friendly': True, 'fenced-code-blocks': pygments_options, 'footnotes': True, 'math_delimiter': math_delimiter if is_math else None})
        # Process <code lang="programming-lang"></code> blocks or spans.
        self.content = self._format_code_lang(self.content)
        self.html = format(template, site_title=config["title"], title=self.title, date=self.date.strftime('%Y-%m-%d'), monthly_archive_url=os.path.dirname(self.permalink) + '/', year=self.date.strftime('%Y'), month=self.date.strftime(
            '%B'), day=self.date.strftime('%d'), tags=', '.join('<a href="/tags/{tag}">{tag}</a>'.format(tag=tag) for tag in self.tags), permalink=self.permalink, excerpt=self.excerpt, content=self.content)
        # Load MathJax for post with math tag.
        if is_math:
            self.html = self.html.replace('</head>', '''
<script type="text/x-mathjax-config">
MathJax.Hub.Config({
  asciimath2jax: {
    delimiters: [['%s','%s']]
  }
});
</script>
<script type="text/javascript" src="http://cdn.mathjax.org/mathjax/latest/MathJax.js?config=TeX-MML-AM_HTMLorMML"></script>
</head>''' % (math_delimiter, math_delimiter))

    @property
    def file_name(self):
        return os.path.basename(self.file_path)

    def __str__(self):
        return '{title}({date})'.format(title=self.title, date=self.pretty_date)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.date == other.date and self.file_name == other.file_name

    def __lt__(self, other):
        if self.date < other.date:
            return True
        elif self.date > other.date:
            return False
        else:
            return self.file_name < other.file_name

    _code_span_re = re.compile(r"""
        <code               # start tag
        \s+                 # word break
        lang=(['"])(\w+)\1  # lang \2
        \s*?>               # closing tag
        (.*?)               # code, minimally matching \3
        </code>             # the matching end tag
    """,
                               re.X | re.M)

    def _code_span_sub(self, match):
        lang = match.group(2)
        code = match.group(3)
        lexer = self._get_pygments_lexer(lang)
        if lexer:
            return self._color_with_pygments(code, lexer)
        else:
            return match.group(0)

    def _format_code_lang(self, text):
        return self._code_span_re.sub(self._code_span_sub, text)

    def _get_pygments_lexer(self, lexer_name):
        try:
            from pygments import lexers, util
        except ImportError:
            return None
        try:
            return lexers.get_lexer_by_name(lexer_name)
        except util.ClassNotFound:
            return None

    def _color_with_pygments(self, code, lexer):
        import pygments
        import pygments.formatters

        class HtmlCodeFormatter(pygments.formatters.HtmlFormatter):

            def _wrap_code(self, inner):
                """A function for use in a Pygments Formatter which
                wraps in <code> tags.
                """
                yield 0, "<code>"
                for tup in inner:
                    yield tup[0], tup[1].strip()
                yield 0, "</code>"

            def wrap(self, source, outfile):
                """Return the source with a code."""
                return self._wrap_code(source)

        formatter = HtmlCodeFormatter(**pygments_options)
        return pygments.highlight(code, lexer, formatter)


@total_ordering
class Tag(object):

    def __init__(self, name, posts):
        self.name = name
        self.posts = posts
        self.path = ('tags/' + name + '/')
        url_comps = urllib.parse.urlparse(posts[0].permalink)
        self.permalink = urllib.parse.urlunparse(
            url_comps[:2] + (self.path,) + (None,) * 3)

    def build_index(self, templates_dir):
        with codecs.open(os.path.join(templates_dir, "tag_archive.html"), 'r', 'utf-8') as f:
            template = f.read()
        posts_match = _posts_re.search(template)
        post_template = posts_match.group(1)
        header_template = template[:posts_match.start()]
        header = format(header_template, site_title=config[
                        "title"], archive_title=self.name)
        post_list = []
        for post in sorted(self.posts, reverse=True):
            if not post:
                break
            post_list.append(format(post_template, title=post.title, date=post.date.strftime(
                '%Y-%m-%d'), pretty_date=post.pretty_date, permalink=post.permalink, excerpt=post.excerpt))
        index = header + ''.join(post_list) + template[posts_match.end():]
        return index

    def __str__(self):
        return '{name}\n{posts}'.format(name=self.name, posts=self.posts)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.name.lower() == other.name.lower()

    def __lt__(self, other):
        return self.name.lower() < other.name.lower()


@total_ordering
class MonthlyArchive(object):

    def __init__(self, month, posts):
        self.month = month
        self.posts = posts
        self.path = os.path.dirname(posts[0].path) + '/'
        self.permalink = os.path.dirname(posts[0].permalink) + '/'

    def build_index(self, templates_dir, prev_archive=None, next_archive=None):
        with codecs.open(os.path.join(templates_dir, "monthly_archive.html"), 'r', 'utf-8') as f:
            template = f.read()
        posts_match = _posts_re.search(template)
        header_template = template[:posts_match.start()]
        prev_archive_title = ''
        prev_archive_url = ''
        if prev_archive:
            prev_archive_title = '<'
            prev_archive_url = prev_archive.permalink
        next_archive_title = ''
        next_archive_url = ''
        if next_archive:
            next_archive_title = '>'
            next_archive_url = next_archive.permalink
        header = format(header_template, site_title=config["title"], archive_title=self.month.strftime('%B, %Y'), prev_archive_title=prev_archive_title, prev_archive_url=prev_archive_url,
                        next_archive_title=next_archive_title, next_archive_url=next_archive_url, month=self.month.strftime('%B'), year=self.month.strftime('%Y'), yearly_archive_url=os.path.dirname(self.permalink[:-1]) + '/')
        post_template = posts_match.group(1)
        post_list = []
        for post in self.posts:
            post_list.append(format(post_template, title=post.title, date=post.date.strftime(
                '%Y-%m-%d'), pretty_date=post.pretty_date, permalink=post.permalink, excerpt=post.excerpt))
        index = header + ''.join(post_list) + template[posts_match.end():]
        return index

    def __str__(self):
        return '{month}\n{posts}'.format(month=self.month.strftime('%Y-%m'), posts=self.posts)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.month == other.month

    def __lt__(self, other):
        return self.month < other.month


@total_ordering
class YearlyArchive(object):

    def __init__(self, year, monthly_archives):
        self.year = year
        self.monthly_archives = monthly_archives
        self.path = os.path.dirname(monthly_archives[0].path[:-1]) + '/'
        self.permalink = os.path.dirname(
            monthly_archives[0].permalink[:-1]) + '/'

    def build_index(self, templates_dir, prev_archive=None, next_archive=None):
        with codecs.open(os.path.join(templates_dir, "yearly_archive.html"), 'r', 'utf-8') as f:
            template = f.read()
        monthly_archives_match = _monthly_archives_re.search(template)
        header_template = template[:monthly_archives_match.start()]
        prev_archive_title = ''
        prev_archive_url = ''
        if prev_archive:
            prev_archive_title = '<'
            prev_archive_url = prev_archive.permalink
        next_archive_title = ''
        next_archive_url = ''
        if next_archive:
            next_archive_title = '>'
            next_archive_url = next_archive.permalink
        header = format(header_template, site_title=config["title"], archive_title=self.year.strftime(
            '%Y'), prev_archive_title=prev_archive_title, prev_archive_url=prev_archive_url, next_archive_title=next_archive_title, next_archive_url=next_archive_url)
        monthly_archive_template = monthly_archives_match.group(1)
        posts_match = _posts_re.search(monthly_archive_template)
        monthly_archive_header = monthly_archive_template[:posts_match.start()]
        post_template = posts_match.group(1)
        monthly_archive_footer = monthly_archive_template[posts_match.end():]
        monthly_archive_list = []
        for monthly_archive in self.monthly_archives:
            post_list = []
            for post in monthly_archive.posts:
                post_list.append(format(post_template, title=post.title, date=post.date.strftime(
                    '%Y-%m-%d'), pretty_date=post.pretty_date, permalink=post.permalink, excerpt=post.excerpt))
            monthly_archive_list.append(format(monthly_archive_header, monthly_archive_title=monthly_archive.month.strftime(
                '%B'), monthly_archive_url=monthly_archive.permalink) + ''.join(post_list) + monthly_archive_footer)
        index = header + ''.join(monthly_archive_list) + \
            template[monthly_archives_match.end():]
        return index

    def __str__(self):
        return '{year}\n{monthly_archives}'.format(year=self.year.strftime('%Y'), monthly_archives=self.monthly_archives)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.year == other.year

    def __lt__(self, other):
        return self.year < other.year


@total_ordering
class TimelineArchive(object):

    def __init__(self, index, posts):
        self.index = index
        self.posts = posts
        self.path = ('archive/' + str(index) + '/') if index > 0 else ''
        url_comps = urllib.parse.urlparse(posts[0].permalink)
        self.permalink = urllib.parse.urlunparse(
            url_comps[:2] + (self.path,) + (None,) * 3)

    def build_index(self, templates_dir, prev_archive=None, next_archive=None):
        with codecs.open(os.path.join(templates_dir, "index.html"), 'r', 'utf-8') as f:
            template = f.read()
        posts_match = _posts_re.search(template)
        header_template = template[:posts_match.start()]
        header = format(header_template,
                        site_description=config["description"])
        footer_template = template[posts_match.end():]
        prev_archive_title = ''
        prev_archive_url = ''
        if prev_archive:
            prev_archive_title = '<'
            prev_archive_url = prev_archive.permalink
        next_archive_title = ''
        next_archive_url = ''
        if next_archive:
            next_archive_title = '>'
            next_archive_url = next_archive.permalink
        footer = format(footer_template, prev_archive_title=prev_archive_title, prev_archive_url=prev_archive_url,
                        next_archive_title=next_archive_title, next_archive_url=next_archive_url)
        post_template = posts_match.group(1)
        post_list = []
        for post in self.posts:
            if not post:
                break
            post_list.append(format(post_template, title=post.title, date=post.date.strftime(
                '%Y-%m-%d'), pretty_date=post.pretty_date, permalink=post.permalink, excerpt=post.excerpt))
        index = header + ''.join(post_list) + footer
        return index

    def __str__(self):
        return '{index}\n{posts}'.format(index=self.index, posts=self.posts)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return self.index == other.index

    def __lt__(self, other):
        return self.index < other.index


class Struct(object):
    '''http://docs.python.org/3/tutorial/classes.html#odds-and-ends'''
    pass


_posts_re = re.compile(r'{{#posts}}(.*){{/posts}}', re.S)
_tags_re = re.compile(r'{{#tags}}(.*){{/tags}}', re.S)
_monthly_archives_re = re.compile(
    r'{{#monthly_archives}}(.*){{/monthly_archives}}', re.S)
_items_re = re.compile(r'{{#items}}(.*){{/items}}', re.S)


def triplepwise(iterable):
    "s -> (s0,s1,s2, (s1,s2,s3), (s2,s3,s4), ..."
    a, b, c = itertools.tee(iterable, 3)
    next(b, None)
    next(c, None)
    next(c, None)
    return zip(a, b, c)


def grouper(n, iterable, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper(3, 'ABCDEFG', 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)


posts = {}
timeline_archives = []
monthly_archives = {}
yearly_archives = {}
tags = {}


def main():
    global published_dir

    # Command line arguments parsing
    cmdln_desc = 'A markdown based blog system.'
    if argparse:
        usage = " %(prog)s PUBLISHED_DIR"
        version = "%(prog)s " + __version__
        parser = argparse.ArgumentParser(
            prog="letterpress", description=cmdln_desc, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("published_dir", metavar="PUBLISHED_DIR")
        parser.add_argument("-v", "--verbose", dest="log_level",
                                  action="store_const", const=logging.DEBUG,
                                  help="more verbose output")
        parser.add_argument("--version", action="version", version=version)
        parser.set_defaults(log_level=logging.INFO)
        options = parser.parse_args()
        published_dir = options.published_dir
    else:
        usage = " %prog PUBLISHED_DIR"
        version = "%prog " + __version__
        parser = optparse.OptionParser(prog="letterpress", usage=usage,
                                       version=version, description=cmdln_desc)
        parser.add_option("-v", "--verbose", dest="log_level",
                                action="store_const", const=logging.DEBUG,
                                help="more verbose output")
        parser.set_defaults(log_level=logging.INFO)
        options, args = parser.parse_args()
        if len(args) != 1:
            parser.print_help()
            return
        published_dir = args[0]
    published_dir = os.path.normpath(published_dir)
    templates_dir = os.path.join(published_dir, 'templates')

    global common_head
    global common_header
    with codecs.open(os.path.join(templates_dir, "common_head.html"), 'r', 'utf-8') as f:
        common_head = f.read()
    with codecs.open(os.path.join(templates_dir, "common_header.html"), 'r', 'utf-8') as f:
        common_header = f.read()

    logger.setLevel(options.log_level)

    # Logging.
    logging_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging_formatter)
    log_file = 'letterpress.log'
    # file_handler = logging.handlers.TimedRotatingFileHandler(os.path.join(published_dir, 'letterpress.log'), when='D', interval=1, backupCount=7, utc=True)
    file_handler = logging.handlers.RotatingFileHandler(os.path.join(
        published_dir, log_file), maxBytes=64 * 1024, backupCount=3)
    file_handler.setFormatter(logging_formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

    # Letterpress config file parsing.
    def read_config():
        global config
        config = {'markdown_ext': '.md'}
        with codecs.open(os.path.join(published_dir, 'letterpress.config'), 'r', 'utf-8') as config_file:
            for line in config_file.readlines():
                line = line.strip()
                if len(line) == 0 or line.startswith('#'):
                    continue
                key, value = line.split(':', 1)
                config[key.strip()] = value.strip()
        logger.info('Site configure: %s', config)

    read_config()

    site_dir = config['site_dir']
    if not os.path.isabs(site_dir):
        site_dir = os.path.join(published_dir, os.path.expanduser(site_dir))
    site_dir = os.path.normpath(site_dir)

    # Clean up old files.
    for rel_path in os.listdir(site_dir):
        path = os.path.join(site_dir, rel_path)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except:
                logger.exception('Can not delete %s', path)

    # Initial complete site building.
    def create_post(file_path):
        post = Post(file_path, base_url=config['base_url'], templates_dir=templates_dir, date_format=config[
                    'date_format'], math_delimiter=config.get('math_delimiter', '$'))
        if not post:
            return None
        output_file_path = os.path.join(site_dir, post.path)
        output_dir = os.path.dirname(output_file_path)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(post.html)
        # html will never be used again. So let's get rid off it to spare some
        # memory.
        del post.html
        return post

    def create_tags(posts):
        global tags
        tags.clear()
        posts_of_tags = {}
        sorted_posts = sorted(posts.values())
        for post in sorted_posts:
            for tag_name in post.tags:
                posts_of_tag = posts_of_tags.get(tag_name)
                if posts_of_tag:
                    posts_of_tag.append(post)
                else:
                    posts_of_tags[tag_name] = [post]
        for tag_name, tag_posts in posts_of_tags.items():
            tag = Tag(tag_name, tag_posts)
            tags[tag_name] = tag
            create_tag_index(tag)

        with codecs.open(os.path.join(templates_dir, "tags.html"), 'r', 'utf-8') as f:
            template = f.read()
        tags_match = _tags_re.search(template)
        header_template = template[:tags_match.start()]
        header = format(header_template, site_title=config["title"])
        tags_template = tags_match.group(1)
        tag_list = []
        for tag in sorted(tags.values()):
            post_count = len(tag.posts)
            tag_list.append(format(tags_template, tag_title=tag.name, tag_url=tag.permalink, tag_size=str(
                len(tag.posts)) + ' ' + ('Articles' if post_count > 1 else 'Article')))
        index = header + ''.join(tag_list) + template[tags_match.end():]
        output_dir = os.path.join(site_dir, 'tags')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_tag_index(tag):
        index = tag.build_index(templates_dir)
        output_dir = os.path.join(site_dir, tag.path)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_timeline_archives(posts):
        global timeline_archives
        del timeline_archives[:]
        sorted_posts = sorted(posts.values(), reverse=True)
        archive_list = [None]
        posts_per_page = int(config.get('posts_per_page', '10'))
        for index, post_group in enumerate(grouper(posts_per_page, sorted_posts)):
            archive = TimelineArchive(index, post_group)
            timeline_archives.append(archive)
            archive_list.append(archive)
        archive_list.append(None)
        for next_archive, archive, prev_archive in triplepwise(archive_list):
            create_timeline_index(archive, prev_archive, next_archive)

    def create_timeline_index(archive, prev_archive, next_archive):
        index = archive.build_index(templates_dir, prev_archive, next_archive)
        output_dir = os.path.join(site_dir, archive.path)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_monthly_archives(posts):
        global monthly_archives
        monthly_archives.clear()
        archive_list = [None]
        month = datetime.date.min
        posts_of_month = []
        # Append a sentry to the end to make code below simpler.
        sentry = Struct()
        sentry.date = datetime.date.max
        sorted_posts = itertools.chain(sorted(posts.values()), [sentry])
        for post in sorted_posts:
            date_of_post = post.date
            month_of_post = datetime.date(
                date_of_post.year, date_of_post.month, 1)
            if month_of_post > month:
                if posts_of_month:
                    archive = MonthlyArchive(month, posts_of_month)
                    monthly_archives[month] = archive
                    archive_list.append(archive)
                month = month_of_post
                posts_of_month = [post]
            else:
                posts_of_month.append(post)
        archive_list.append(None)
        for prev_archive, archive, next_archive in triplepwise(archive_list):
            create_monthly_index(archive, prev_archive, next_archive)

    def create_monthly_index(archive, prev_archive, next_archive):
        index = archive.build_index(templates_dir, prev_archive, next_archive)
        output_dir = os.path.join(site_dir, archive.path)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_yearly_archives(monthly_archives):
        global yearly_archives
        yearly_archives.clear()
        archive_list = [None]
        year = datetime.date.min
        archives_of_year = []
        # Append a sentry to the end to make code below simpler.
        sentry = Struct()
        sentry.month = datetime.date.max
        sorted_monthly_archives = itertools.chain(
            sorted(monthly_archives.values()), [sentry])
        for monthly_archive in sorted_monthly_archives:
            month_of_archive = monthly_archive.month
            year_of_archive = datetime.date(month_of_archive.year, 1, 1)
            if year_of_archive > year:
                if archives_of_year:
                    archive = YearlyArchive(year, archives_of_year)
                    yearly_archives[year] = archive
                    archive_list.append(archive)
                year = year_of_archive
                archives_of_year = [monthly_archive]
            else:
                archives_of_year.append(monthly_archive)
        archive_list.append(None)
        for prev_archive, archive, next_archive in triplepwise(archive_list):
            create_yearly_index(archive, prev_archive, next_archive)

    def create_yearly_index(archive, prev_archive, next_archive):
        index = archive.build_index(templates_dir, prev_archive, next_archive)
        output_dir = os.path.join(site_dir, archive.path)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_complete_archive(monthly_archives):
        with codecs.open(os.path.join(templates_dir, "archive.html"), 'r', 'utf-8') as f:
            template = f.read()
        monthly_archives_match = _monthly_archives_re.search(template)
        header_template = template[:monthly_archives_match.start()]
        header = format(header_template, site_title=config["title"])
        monthly_archive_template = monthly_archives_match.group(1)
        posts_match = _posts_re.search(monthly_archive_template)
        monthly_archive_header = monthly_archive_template[:posts_match.start()]
        post_template = posts_match.group(1)
        monthly_archive_footer = monthly_archive_template[posts_match.end():]
        monthly_archive_list = []
        for monthly_archive in sorted(monthly_archives.values(), reverse=True):
            post_list = []
            for post in reversed(monthly_archive.posts):
                post_list.append(format(post_template, title=post.title, date=post.date.strftime(
                    '%Y-%m-%d'), pretty_date=post.pretty_date, permalink=post.permalink, excerpt=post.excerpt))
            monthly_archive_list.append(format(monthly_archive_header, monthly_archive_title=monthly_archive.month.strftime(
                '%B, %Y'), monthly_archive_url=monthly_archive.permalink) + ''.join(post_list) + monthly_archive_footer)
        index = header + ''.join(monthly_archive_list) + \
            template[monthly_archives_match.end():]
        output_dir = os.path.join(site_dir, 'archive')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        output_file_path = os.path.join(output_dir, 'index.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(index)

    def create_404_page():
        with codecs.open(os.path.join(templates_dir, "404.html"), 'r', 'utf-8') as f:
            template = f.read()
        page = format(template, site_title=html.escape(config["title"]))
        output_file_path = os.path.join(site_dir, '404.html')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(page)

    def create_rss_feed(posts):
        with codecs.open(os.path.join(templates_dir, "feed.xml"), 'r', 'utf-8') as f:
            template = f.read()
        items_match = _items_re.search(template)
        item_template = items_match.group(1)
        item_list = []

        sorted_posts = sorted(posts.values(), reverse=True)
        for post in sorted_posts:
            item_list.append(format(item_template, title=post.title, date=email.utils.format_datetime(
                post.date), permalink=post.permalink, content=post.content))
        feed = format(template[:items_match.start()], site_title=html.escape(config["title"]), site_description=html.escape(
            config["description"]), site_link=config["base_url"]) + ''.join(item_list) + template[items_match.end():]

        output_file_path = os.path.join(site_dir, 'feed.xml')
        with codecs.open(output_file_path, 'w', 'utf-8') as output_file:
            output_file.write(feed)

    def build_site():
        logger.info('Build site')
        global posts
        posts.clear()
        for rel_path in os.listdir(published_dir):
            path = os.path.join(published_dir, rel_path)
            basename = os.path.basename(path)
            if os.path.splitext(basename)[1] == config['markdown_ext']:
                # Post.
                post = create_post(path)
                if post:
                    posts[post.file_path] = post
            elif basename == 'letterpress.config':
                pass
            elif os.path.normpath(path) == templates_dir:
                pass
            elif basename.startswith(log_file) or basename.startswith('.'):
                pass
            else:
                # Resource.
                if site_dir == published_dir:
                    continue
                dst = os.path.join(site_dir, basename)
                if os.path.isdir(path):
                    if os.path.exists(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    try:
                        shutil.copytree(path, dst)
                    except Exception as e:
                        logger.exception('Can not copytree')
                else:
                    try:
                        shutil.copyfile(path, dst)
                    except Exception as e:
                        logger.exception('Can not copyfile')
        create_tags(posts)
        create_timeline_archives(posts)
        create_monthly_archives(posts)
        create_yearly_archives(monthly_archives)
        create_complete_archive(monthly_archives)
        create_404_page()
        create_rss_feed(posts)

    build_site()

    # Continuous posts monitoring and site building.
    class ResourceChangeHandler(pyinotify.PrintAllEvents):

        def process_default(self, event):
            if event.name.startswith(log_file):
                return
            # super(ResourceChangeHandler, self).process_default(event)
            file_create_mask = pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO
            dir_create_mask = pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO
            delete_mask = pyinotify.IN_DELETE | pyinotify.IN_MOVED_FROM
            path = os.path.normpath(event.path)
            if path == published_dir:
                if not event.dir:
                    if os.path.basename(event.pathname) == 'letterpress.config':
                        # Configure file changed. Rebuild the whole site.
                        if event.mask & file_create_mask:
                            logger.info('New site configure')
                            read_config()
                            build_site()
                        return
                    elif os.path.splitext(event.pathname)[1] == config['markdown_ext']:
                        if event.mask & file_create_mask:
                            # New post or post changed.
                            post = create_post(event.pathname)
                            if not post:
                                return
                            if post.file_path in posts:
                                logger.info('Update post: %s',
                                            os.path.basename(event.pathname))
                            else:
                                logger.info('New post: %s',
                                            os.path.basename(event.pathname))
                            posts[post.file_path] = post
                            create_tags(posts)
                            create_timeline_archives(posts)
                            create_monthly_archives(posts)
                            create_yearly_archives(monthly_archives)
                            create_complete_archive(monthly_archives)
                            create_rss_feed(posts)
                        elif event.mask & delete_mask:
                            # Delete post.
                            logger.info('Delete post: %s',
                                        os.path.basename(event.pathname))
                            post = posts.pop(event.pathname, None)
                            if post:
                                dst = os.path.join(site_dir, post.path)
                                if os.path.exists(dst):
                                    try:
                                        os.remove(dst)
                                    except:
                                        logger.exception(
                                            'Can not delete %s', dst)
                                for tag_name in post.tags:
                                    tag = tags.get(tag_name)
                                    if tag:
                                        try:
                                            tag.posts.remove(post)
                                        except:
                                            pass
                                        if not tag.posts:
                                            # The tag is empty now. Remove it.
                                            tags.pop(tag_name, None)
                                            dst = os.path.join(
                                                site_dir, tag.path)
                                            if os.path.exists(dst):
                                                shutil.rmtree(
                                                    dst, ignore_errors=True)
                                posts_per_page = int(
                                    config.get('posts_per_page', '10'))
                                if len(posts) % posts_per_page == 0 and len(timeline_archives) > 1:
                                    # Last timeline archive is empty. Remove
                                    # it.
                                    last_timeline_archive = timeline_archives.pop()
                                    dst = os.path.join(
                                        site_dir, last_timeline_archive.path)
                                    if os.path.exists(dst):
                                        shutil.rmtree(dst, ignore_errors=True)

                                monthly_archive = monthly_archives.get(
                                    datetime.date(post.date.year, post.date.month, 1))
                                if monthly_archive:
                                    try:
                                        monthly_archive.posts.remove(post)
                                    except:
                                        pass
                                    if not monthly_archive.posts:
                                        # The month is empty now. Remove it.
                                        monthly_archives.pop(
                                            monthly_archive.month, None)
                                        dst = os.path.join(
                                            site_dir, monthly_archive.path)
                                        if os.path.exists(dst):
                                            shutil.rmtree(
                                                dst, ignore_errors=True)
                                        yearly_archive = yearly_archives.get(
                                            datetime.date(monthly_archive.month.year, 1, 1))
                                        if yearly_archive:
                                            try:
                                                yearly_archive.monthly_archives.remove(
                                                    monthly_archive)
                                            except:
                                                pass
                                            if not yearly_archive.monthly_archives:
                                                # The year is empty now. Remove
                                                # it.
                                                yearly_archives.pop(
                                                    yearly_archive.year, None)
                                                dst = os.path.join(
                                                    site_dir, yearly_archive.path)
                                                if os.path.exists(dst):
                                                    shutil.rmtree(
                                                        dst, ignore_errors=True)

                                create_tags(posts)
                                create_timeline_archives(posts)
                                create_monthly_archives(posts)
                                create_yearly_archives(monthly_archives)
                                create_complete_archive(monthly_archives)
                                create_rss_feed(posts)
                        return
            elif path == templates_dir:
                # Template changed. Rebuild the whole site.
                if event.mask & file_create_mask:
                    logger.info('Update template: %s',
                                os.path.basename(event.pathname))
                    build_site()
                return
            # Map other resource changes into site dir.
            if site_dir == published_dir:
                return
            if os.path.basename(event.pathname).startswith('.'):
                # Ignore hidden/temp files
                return
            rel_path = os.path.relpath(event.pathname, published_dir)
            dst = os.path.join(site_dir, rel_path)
            if event.dir:
                if event.mask & dir_create_mask:
                    logger.info('New resource dir: %s', rel_path)
                    if os.path.exists(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    try:
                        shutil.copytree(event.pathname, dst)
                    except Exception as e:
                        logger.exception('Can not copytree')
                elif event.mask & delete_mask:
                    logger.info('Delete resource dir: %s', rel_path)
                    if os.path.exists(dst):
                        shutil.rmtree(dst, ignore_errors=True)
            else:
                if event.mask & file_create_mask:
                    logger.info('New resource file: %s', rel_path)
                    try:
                        shutil.copyfile(event.pathname, dst)
                    except Exception as e:
                        logger.exception('Can not copyfile')
                elif event.mask & delete_mask:
                    logger.info('Delete resource file: %s', rel_path)
                    if os.path.exists(dst):
                        try:
                            os.remove(dst)
                        except:
                            logger.exception('Can not delete %s', dst)

    wm = pyinotify.WatchManager()
    mask = pyinotify.ALL_EVENTS
    notifier = pyinotify.Notifier(wm)
    wm.add_watch(published_dir, mask,
                 proc_fun=ResourceChangeHandler(), rec=True, auto_add=True)
    notifier.loop()

if __name__ == "__main__":
    sys.exit(main())
