#! /usr/bin/python

import calendar
import cgi
import json
import MySQLdb as mdb
import re
import sys
import subprocess
import datetime
from os import environ

import config

def convert_to_builtin_type(obj):
    return str(obj)

class Cache:
    """Cache"""

    cache = {}

    def put(self, entity_type, key, value):
        """adds an entry to the cache"""

        if not entity_type in self.cache:
            self.cache[entity_type] = {}
        self.cache[entity_type][key] = value


    def get(self, entity_type, key):
        """gets an entry from the cache"""

        if not entity_type in self.cache:
            return None
        return self.cache[entity_type][key]


    def has(self, entity_type, key):
        """checks whether an item is in the cache"""

        if not entity_type in self.cache:
            return False
        return key in self.cache[entity_type]


class PostsaiDB:
    """Database access for postsai"""

    column_table_mapping = {
        "repository" : "repositories",
        "who" : "people",
        "dir" : "dirs",
        "file" : "files",
        "branch" : "branches",
        "description" : "descs",
        "hash": "commitids"
    }


    def __init__(self, config):
        """Creates a Postsai api instance"""

        self.config = config


    def connect(self):
        self.conn = mdb.connect(
            host    = self.config["db"]["host"],
            user    = self.config["db"]["user"],
            passwd  = self.config["db"]["password"],
            db      = self.config["db"]["database"],
            port    = self.config["db"].get("port", 3306),
            use_unicode = True,
            charset = "utf8")

        # checks whether this is a ViewVC database instead of a Bonsai database
        cursor = self.conn.cursor()
        cursor.execute("show tables like 'commits'")
        self.is_viewvc_database = (cursor.rowcount == 1)
        cursor.execute("SET SESSION innodb_lock_wait_timeout = 500")
        cursor.close()
        self.conn.begin()


    def disconnect(self):
        self.conn.commit()
        self.conn.close()


    def rewrite_sql(self, sql):
        if self.is_viewvc_database:
            sql = sql.replace("checkins", "commits")
        return sql


    def query(self, sql, data, cursor_type=None):
        cursor = self.conn.cursor(cursor_type)
        cursor.execute(self.rewrite_sql(sql), data)
        rows = cursor.fetchall()
        cursor.close()
        return rows


    def query_as_double_map(self, sql, key, data=None):
        rows = self.query(sql, data, mdb.cursors.DictCursor)
        res = {}
        for row in rows:
            res[row[key]] = row
        return res


    @staticmethod
    def guess_repository_urls(row):
        """guesses the repository urls"""

        base = row["url"]
        base_url = base
        if (base_url.find(row["repository"]) == -1):
            base_url = base_url + "/" + row["repository"]

        file_url = ""
        commit_url = ""
        tracker_url = ""
        icon_url = ""
        repository_url = row["repository_url"]

        # GitHub, Gitlab
        if base_url.find("https://github.com/") > -1 or base_url.find("gitlab") > -1:
            commit_url = base_url + "/commit/[commit]"
            file_url = base_url + "/blob/[commit]/[file]"
            tracker_url = base_url + "/issues/$1"

        # SourceForge
        elif base_url.find("://sourceforge.net") > -1:
            if row["revision"].find(".") == -1 and len(row["revision"]) < 30:  # Subversion
                commit_url = "https://sourceforge.net/[repository]/[commit]/"
                file_url = "https://sourceforge.net/[repository]/[commit]/tree/[file]"
            else: # CVS, Git
                commit_url = "https://sourceforge.net/[repository]/ci/[commit]/"
                file_url = "https://sourceforge.net/[repository]/ci/[revision]/tree/[file]"
            icon_url = "https://a.fsdn.com/allura/[repository]/../icon"

        # CVS
        elif row["revision"].find(".") > -1:  # CVS
            commit_url = "commit.html?repository=[repository]&commit=[commit]"
            file_url = base + "/[repository]/[file]?revision=[revision]&view=markup"

        # Git
        else: # git instaweb
            commit_url = base + "/?p=[repository];a=commitdiff;h=[commit]"
            file_url = base + "/?p=[repository];a=blob;f=[file];hb=[commit]"

        return (base_url, repository_url, file_url, commit_url, tracker_url, icon_url)


    def call_setup_repository(self, row, guess):
        """let the configruation overwrite guessed repository info"""

        if not "setup_repository" in self.config:
            return guess
        return self.config["setup_repository"](row, *guess)


    def extra_data_for_key_tables(self, cursor, column, row, value):
        """provides additional data that should be stored in lookup tables"""

        extra_column = ""
        extra_data = ""
        data = [value]
        if column == "description":
            extra_column = ", hash"
            extra_data = ", %s"
            data.append(len(value))
        elif column == "repository":
            extra_column = ", base_url, repository_url, file_url, commit_url, tracker_url, icon_url"
            extra_data = ", %s, %s, %s, %s, %s, %s"
            data.extend(self.call_setup_repository(row, self.guess_repository_urls(row)))
        elif column == "hash":
            extra_column = ", authorid, committerid, co_when"
            extra_data = ", %s, %s, %s"
            self.fill_id_cache(cursor, "who", row, row["author"])
            self.fill_id_cache(cursor, "who", row, row["committer"])
            data.extend((self.cache.get("who", row["author"]),
                         self.cache.get("who", row["committer"]),
                         row["co_when"]))

        return data, extra_column, extra_data


    def fill_id_cache(self, cursor, column, row, value):
        """fills the id-cache"""

        if self.cache.has(column, value):
            return

        data, extra_column, extra_data = self.extra_data_for_key_tables(cursor, column, row, value)

        sql = "SELECT id FROM " + self.column_table_mapping[column] + " WHERE " + column + " = %s"
        cursor.execute(sql, [value])
        rows = cursor.fetchall()
        if len(rows) > 0:
            self.cache.put(column, value, rows[0][0])
        else:
            sql = "INSERT INTO " + self.column_table_mapping[column] + " (" + column + extra_column + ") VALUE (%s" + extra_data + ")"
            cursor.execute(sql, data)
            self.cache.put(column, value, cursor.lastrowid)


    def import_data(self, head, rows):
        """Imports data"""

        self.connect()
        self.cache = Cache()
        cursor = self.conn.cursor()

        sql = """INSERT INTO importactions (remote_addr, remote_user, sender_addr, sender_user, ia_when) VALUES (%s, %s, %s, %s, %s)"""
        cursor.execute(sql, [
            environ.get("REMOTE_ADDR", ""), environ.get("REMOTE_USER", ""),
            head["sender_addr"],head["sender_user"],
            datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        ])
        importactionid = cursor.lastrowid

        for row in rows:
            for key in self.column_table_mapping:
                self.fill_id_cache(cursor, key, row, row[key])

        for row in rows:
            sql = """INSERT IGNORE INTO checkins(type, ci_when, whoid, repositoryid, dirid, fileid, revision, branchid, addedlines, removedlines, descid, stickytag, commitid, importactionid)
                 VALUE (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
            cursor.execute(self.rewrite_sql(sql), [
                row["type"],
                row["ci_when"],
                self.cache.get("who", row["who"]),
                self.cache.get("repository", row["repository"]),
                self.cache.get("dir", row["dir"]),
                self.cache.get("file", row["file"]),
                row["revision"],
                self.cache.get("branch", row["branch"]),
                row["addedlines"],
                row["removedlines"],
                self.cache.get("description", row["description"]),
                "",
                self.cache.get("hash", row["commitid"]),
                str(importactionid)
                ])

        cursor.close()
        self.disconnect()



class Postsai:

    def __init__(self, config):
        """Creates a Postsai api instance"""

        self.config = config


    def validate_input(self, form):
        """filter inputs, e. g. for privacy reasons"""

        if not "filter" in self.config:
            return ""

        for key, condition_filter in self.config['filter'].items():
            value = form.getfirst(key, "")
            if value != "":
                if value.startswith("^") and value.endswith("$"):
                    value = value[1:-1]
                if re.match(condition_filter, value) == None:
                    return "Missing permissions for query on column \"" + key + "\""

        return ""


    def get_read_permission_pattern(self):
        """get read permissions pattern"""

        if not "get_read_permission_pattern" in self.config:
            return ".*"
        return self.config["get_read_permission_pattern"]()


    def create_query(self, form):
        """creates the sql statement"""

        self.data = [self.get_read_permission_pattern()]
        self.sql = """SELECT repositories.repository, checkins.ci_when, people.who, trim(leading '/' from concat(concat(dirs.dir, '/'), files.file)),
        revision, branches.branch, concat(concat(checkins.addedlines, '/'), checkins.removedlines), descs.description, repositories.repository, commitids.hash 
        FROM checkins 
        JOIN branches ON checkins.branchid = branches.id
        JOIN descs ON checkins.descid = descs.id
        JOIN dirs ON checkins.dirid = dirs.id
        JOIN files ON checkins.fileid = files.id
        JOIN people ON checkins.whoid = people.id
        JOIN repositories ON checkins.repositoryid = repositories.id
        LEFT JOIN commitids ON checkins.commitid = commitids.id
        WHERE repositories.repository REGEXP %s """

        self.create_where_for_column("branch", form, "branch")
        self.create_where_for_column("dir", form, "dir")
        self.create_where_for_column("description", form, "description")
        self.create_where_for_column("file", form, "file")
        self.create_where_for_column("who", form, "who")
        self.create_where_for_column("cvsroot", form, "repository")
        self.create_where_for_column("repository", form, "repository")
        self.create_where_for_column("commit", form, "commitids.hash")

        self.create_where_for_date(form)

        self.sql = self.sql + " ORDER BY checkins.ci_when DESC, checkins.branchid DESC, checkins.descid DESC, checkins.id DESC"
        limit = form.getfirst("limit", None)
        if limit:
            self.sql = self.sql + " LIMIT " + str(int(limit))


    @staticmethod
    def convert_operator(matchtype):
        operator = '='
        if (matchtype == "match"):
            operator = '='
        elif (matchtype == "regexp" or matchtype == "search"):
            # support for MySQL <= 5.5
            operator = "REGEXP"
        elif (matchtype == "notregexp"):
            operator = "NOT REGEXP"
        return operator


    def create_where_for_column(self, column, form, internal_column):
        """create the where part for the specified column with data from the request"""

        value = form.getfirst(column, "")
        if (value == ""):
            return ""

        # replace HEAD branch with empty string
        if (column == "branch" and value == "HEAD"):
            value = ""

        matchtype = form.getfirst(column+"type", "match")
        if internal_column == "description" and matchtype == "search" and not self.config["db"].get("old_mysql_version", False):
            self.sql = self.sql + " AND MATCH (" + internal_column + ") AGAINST (%s)"
        else:
            self.sql = self.sql + " AND " + internal_column + " " + self.convert_operator(matchtype) + " %s"
        self.data.append(value)


    def create_where_for_date(self, form):
        """parses the date parameters and adds them to the database query"""

        datetype = form.getfirst("date", "day")
        if (datetype == "none"):
            self.sql = self.sql + " AND 1 = 0"
        elif (datetype == "day"):
            self.sql = self.sql + " AND ci_when >= DATE_SUB(NOW(),INTERVAL 1 DAY)"
        elif (datetype == "week"):
            self.sql = self.sql + " AND ci_when >= DATE_SUB(NOW(),INTERVAL 1 WEEK)"
        elif (datetype == "month"):
            self.sql = self.sql + " AND ci_when >= DATE_SUB(NOW(),INTERVAL 1 MONTH)"
        elif (datetype == "hours"):
            self.sql = self.sql + " AND ci_when >= DATE_SUB(NOW(),INTERVAL %s HOUR)"
            self.data.append(form.getfirst("hours"))
        elif (datetype == "explicit"):
            mindate = form.getfirst("mindate", "")
            if mindate != "":
                self.sql = self.sql + " AND ci_when >= %s"
                self.data.append(mindate)
            maxdate = form.getfirst("maxdate", "")
            if maxdate != "":
                self.sql = self.sql + " AND ci_when <= %s"
                self.data.append(maxdate)

    @staticmethod
    def are_rows_in_same_commit(data, pre):
        return data[9] == pre[9] and data[9] != None



    @staticmethod
    def convert_database_row_to_array(row):
        tmp = []
        for col in row:
            tmp.append(col)
        return tmp


    @staticmethod
    def extract_commits(rows):
        """Merges query result rows to extract commits"""

        result = []
        lastRow = None
        for row in rows:
            tmp = Postsai.convert_database_row_to_array(row)
            tmp[3] = [tmp[3]]
            tmp[4] = [tmp[4]]
            if (lastRow == None):
                lastRow = tmp
                result.append(tmp)
            else:
                if Postsai.are_rows_in_same_commit(lastRow, tmp):
                    lastRow[3].append(tmp[3][0])
                    lastRow[4].append(tmp[4][0])
                else:
                    result.append(tmp)
                    lastRow = tmp

        return result


    def process(self):
        """processes an API request"""

        print("Content-Type: text/json; charset='utf-8'\r")
        print("Cache-Control: max-age=60\r")
        print("\r")
        form = cgi.FieldStorage()

        result = self.validate_input(form)

        if result == "":
            self.create_query(form)

            db = PostsaiDB(self.config)
            db.connect()
            rows = self.extract_commits(db.query(self.sql, self.data))
            repositories = db.query_as_double_map(
                "SELECT id, repository, base_url, file_url, commit_url, tracker_url, icon_url FROM repositories WHERE repositories.repository REGEXP %s",
                "repository",
                [self.get_read_permission_pattern()])
            db.disconnect()

            ui = {}
            if "ui" in vars(config):
                ui = self.config['ui']

            result = {
                "config" : ui,
                "data" : rows,
                "repositories": repositories
            }

        print(json.dumps(result, default=convert_to_builtin_type))



class PostsaiCommitViewer:
    """Reads a commit from a repository"""


    def __init__(self, config):
        """Creates a PostsaiCommitViewer instance"""

        self.config = config


    def read_commit(self, form):
        db = PostsaiDB(self.config)
        db.connect()
        sql = """SELECT repositories.repository, checkins.ci_when, people.who,
            trim(leading '/' from concat(concat(dirs.dir, '/'), files.file)),
            revision, descs.description, commitids.hash, commitids.co_when, repository_url
            FROM checkins 
            JOIN descs ON checkins.descid = descs.id
            JOIN dirs ON checkins.dirid = dirs.id
            JOIN files ON checkins.fileid = files.id
            JOIN people ON checkins.whoid = people.id
            JOIN repositories ON checkins.repositoryid = repositories.id
            JOIN commitids ON checkins.commitid = commitids.id
            WHERE repositories.repository = %s AND commitids.hash = %s """
        data = [form.getfirst("repository", ""), form.getfirst("commit", "")]
        result = db.query(sql, data)
        db.disconnect()
        return result


    @staticmethod
    def format_commit_header(commit):
        """Extracts the commit meta information"""

        result = {
            "repository": commit[0][0],
            "published": commit[0][1],
            "author": commit[0][2],
            "description": commit[0][5],
            "commit": commit[0][6],
            "timestamp": commit[0][7]
        }
        return result


    @staticmethod
    def calculate_previous_cvs_revision(revision):
        split = revision.split(".")
        last = split[len(split) - 1]
        if (last == "1" and len(split) > 2):
            split.pop()
            split.pop()
        else:
            split[len(split) - 1] = str(int(last) - 1)
        return ".".join(split)


    @staticmethod
    def dump_commit_diff(commit):
        for file in commit:
            if file[4] == "" or "." not in file[4]:
                sys.stdout.flush()
                print("Index: " + file[3] + " deleted\r")
                sys.stdout.flush()
            else:
                subprocess.call([
                    "cvs",
                    "-d",
                    file[8],
                    "rdiff",
                    "-u",
                    "-r",
                    PostsaiCommitViewer.calculate_previous_cvs_revision(file[4]),
                    "-r",
                    file[4],
                    file[3]])


    def process(self):
        """Returns information about a commit"""

        print("Content-Type: text/plain; charset='utf-8'\r")
        print("Cache-Control: max-age=60\r")
        print("\r")

        form = cgi.FieldStorage()
        commit = self.read_commit(form)

        print(json.dumps(PostsaiCommitViewer.format_commit_header(commit), default=convert_to_builtin_type))
        sys.stdout.flush()
        PostsaiCommitViewer.dump_commit_diff(commit)



class PostsaiImporter:
    """Imports commits from a webhook"""

    def __init__(self, config, data):
        self.config = config
        self.data = data


    @staticmethod
    def parse_timestamp(t):
        """Parses a timestamp with optional timezone into local time"""

        if len(t) <= 19:
            return t

        parsed = datetime.datetime.strptime(t[0:19],'%Y-%m-%dT%H:%M:%S')
        if t[19]=='+':
            parsed -= datetime.timedelta(hours=int(t[20:22]))
        elif t[19]=='-':
            parsed += datetime.timedelta(hours=int(t[20:22]))
        return datetime.datetime.fromtimestamp(calendar.timegm(parsed.timetuple())).isoformat()



    def check_permission(self, repo_name):
        """checks writes write permissions"""

        if not "get_write_permission_pattern" in self.config:
            return True
        regex = self.config["get_write_permission_pattern"]()
        return not re.match(regex, repo_name) == None


    @staticmethod
    def split_full_path(full_path):
        """splits a full_path into directory and file parts"""

        sep = full_path.rfind("/")
        folder = ""
        if (sep > -1):
            folder = full_path[0:sep]
        file = full_path[sep+1:]
        return folder, file


    def extract_repo_name(self):
        repo = self.data['repository']

        if "full_name" in repo:  # github, sourceforge
            repo_name = repo["full_name"]
        elif "project" in self.data and "path_with_namespace" in self.data["project"]: # gitlab
            repo_name = self.data["project"]["path_with_namespace"]
        else:
            repo_name = repo["name"] # notify-webhook
        return repo_name.strip("/") # sourceforge


    def extract_repo_url(self):
        repo = self.data['repository']
        repository_url = ""

        if "clone_url" in repo:  # github
            repository_url = repo["clone_url"]
        elif "git_ssh_url" in repo: # gitlab
            repository_url = repo["git_ssh_url"]
        elif "url" in repo: # sourceforge, notify-cvs-webhook
            repository_url = repo["url"]
        return repository_url


    def extract_url(self):
        if "project" in self.data and "web_url" in self.data["project"]: # gitlab
            url = self.data["project"]["web_url"]
        elif "home_url" in self.data['repository']:
            url = self.data['repository']["home_url"]
        else:
            url = self.data['repository']["url"]
        return url


    def extract_branch(self):
        """Extracts the branch name, master/HEAD are converted to an empty string."""

        if not "ref" in self.data:
            return ""

        branch = self.data['ref'][self.data['ref'].rfind("/")+1:]
        if branch == "master" or branch == "HEAD":
            return ""
        return branch


    @staticmethod
    def filter_out_folders(files):
        """Sourceforge includes folders in the file list, but we do not want them"""

        result = {}
        for file_to_test, value in files.items():
            for file in files.keys():
                if file.find(file_to_test + "/") == 0:
                    break
            else:
                result[file_to_test] = value
        return result


    @staticmethod
    def extract_files(commit):
        """Extracts a file list from the commit information"""

        result = {}
        actionMap = {
            "added" : "Add",
            "copied": "Add",
            "removed" : "Remove",
            "modified" : "Change"
        }
        for change in ("added", "copied", "removed", "modified"):
            if not change in commit:
                continue
            for full_path in commit[change]:
                result[full_path] = actionMap[change]
        return result


    @staticmethod
    def file_revision(commit, full_path):
        if "revisions" in commit:
            return commit["revisions"][full_path]
        else:
            rev = commit["id"]
            # For Subversion, remove leading r
            if rev[0] == "r":
                rev = rev[1:]
            return rev


    @staticmethod
    def extract_committer(commit):
        if "committer" in commit:
            return commit["committer"]
        else:
            return commit["author"]


    @staticmethod
    def extract_email(author):
        """Use name as replacement for missing or empty email property (Sourceforge Subversion)"""

        if "email" in author and author["email"] != "":
            return author["email"].lower()
        elif "name" in author:
            return author["name"].lower()
        return ""


    def extract_sender_addr(self):
        if "sender" in self.data:
            if "addr" in self.data["sender"]:
                return self.data["sender"]["addr"]
        return ""


    def extract_sender_user(self):
        if "sender" in self.data:
            if "login" in self.data["sender"]:
                return self.data["sender"]["login"]
        if "user_email" in self.data:
            return self.data["user_email"]
        if "user_id" in self.data:
            return self.data["user_id"]
        if "user_name" in self.data:
            return self.data["user_name"]

        return ""


    def parse_data(self):
        """parse webhook data"""

        head = {
            "sender_addr": self.extract_sender_addr(),
            "sender_user": self.extract_sender_user()
        }

        rows = []
        timestamp = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        repo_name = self.extract_repo_name()

        for commit in self.data['commits']:
            if ("replay" in self.data and self.data["replay"]):
                timestamp = self.parse_timestamp(commit["timestamp"])
            for full_path, change_type in self.filter_out_folders(self.extract_files(commit)).items():
                folder, file = self.split_full_path(full_path)
                row = {
                    "type" : change_type,
                    "ci_when" : timestamp,
                    "co_when" : self.parse_timestamp(commit["timestamp"]),
                    "who" : self.extract_email(commit["author"]),
                    "url" : self.extract_url(),
                    "repository" : repo_name,
                    "repository_url" : self.extract_repo_url(),
                    "dir" : folder,
                    "file" : file,
                    "revision" : self.file_revision(commit, full_path),
                    "branch" : self.extract_branch(),
                    "addedlines" : "0",
                    "removedlines" : "0",
                    "description" : commit["message"],
                    "commitid" : commit["id"],
                    "hash" : commit["id"],
                    "author" : self.extract_email(commit["author"]),
                    "committer" : self.extract_email(self.extract_committer(commit))
                }
                rows.append(row)

        return head, rows

    
    def import_from_webhook(self):
        """Import this webhook invokation into the database"""

        repo_name = self.extract_repo_name()
        if not self.check_permission(repo_name):
            print("Status: 403 Forbidden\r")
            print("Content-Type: text/html; charset='utf-8'\r")
            print("\r")
            print("<html><body>Missing permission</body></html>")

        print("Content-Type: text/plain; charset='utf-8'\r")
        print("\r")

        head, rows = self.parse_data()
        db = PostsaiDB(self.config)
        db.import_data(head, rows)
        print("Completed")



if __name__ == '__main__':
    if environ.has_key('REQUEST_METHOD') and environ['REQUEST_METHOD'] == "POST":
        PostsaiImporter(vars(config), json.loads(sys.stdin.read())).import_from_webhook()
    else:
        form = cgi.FieldStorage()
        if form.getfirst("method", "") == "commit":
            PostsaiCommitViewer(vars(config)).process()
        else:
            Postsai(vars(config)).process()

