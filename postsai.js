var $ = window.$ || {};

(function() {
"use strict";

/**
 * areRowMergable?
 */
function areRowsMergable(data, lastGroupStart, index) {
	return (data[index][0] === data[lastGroupStart][0])
		&& (data[index][1].substring(0, 10) === data[lastGroupStart][1].substring(0, 10))
		&& (data[index][2] === data[lastGroupStart][2])
		&& (data[index][5] === data[lastGroupStart][5])
		&& (data[index][7] === data[lastGroupStart][7]);
}

/**
 * merges commit messages on files committed together
 */
function mergeCells(data) {
	var lastGroupStart = 0;
	for (var i = 1; i < data.length; i++) {
		if (!areRowsMergable(data, i, lastGroupStart)) {
			if (lastGroupStart + 1 !== i) {
				$("#table").bootstrapTable("mergeCells", {index: lastGroupStart, field: 7, rowspan: i-lastGroupStart});
			}
			
			lastGroupStart = i;
		}
	}
	var index = data.length - 1;
	if (areRowsMergable(data, index, lastGroupStart)) {
		if (lastGroupStart !== index) {
			$("#table").bootstrapTable("mergeCells", {index: lastGroupStart, field: 7, rowspan: index - lastGroupStart + 1});
		}
	}
}

/**
 * rewrites the links to include the query string
 */
function addQueryStringToLink() {
	$(".action-add-querystring").each(function() {
		var href = this.href;
		if (href.indexOf("?") < 0) {
			$(this).attr("href", this.href + window.location.search);
		}
	});
}

/**
 * extracts parameters from query string
 */
// http://stackoverflow.com/a/20097994
function getUrlVars() {
	var vars = {};
	window.location.href.replace(/[?&]+([^=&]+)=([^&]*)/gi, 
		function(m,key,value) {
			vars[key] = decodeURIComponent(value.replace("+", " "));
		});
	return vars;
}

/**
 * loads URL parameters into search form
 */
function addValuesFromURLs() {
	var params = getUrlVars();
	$("input").each(function() {
		var value = params[this.name];
		if (!value) {
			return;
		}

		if (this.type === "radio") {
			if (this.value === value) {
				$(this).attr("checked", true);
			}
		} else {
			$(this).val(value);
		}
	});
}

/**
 * Is this a primary paramter or a sub-paramter of a selected parent?
 */
function isQueryParameterImportant(vars, key) {
	if (key === "hours") {
		if (vars["date"] !== "hours") {
			return false;
		}
	} else if (key === "mindate" || key === "maxdate") {
		if (vars["date"] !== "explicit") {
			return false;
		}
	} 	
	return true;
}

/**
 * converts the operator parameter into a human readable form
 */
function typeToOperator(type) {
	var operator = "=";
	if (type === "regexp") {
		operator = "~";
	} else if (type === "notregexp") {
		operator = "!~";
	}
	return operator;
}

/**
 * renders a summary of the search query
 */
function renderQueryParameters() {
	$(".search-parameter").each(function() {
		var params = ["Repository", "When", "Who", "Directory", "File", "Rev", "Branch", "Description", "Date", "Hours", "MinDate", "MaxDate"];
		var text = "";
		var vars = getUrlVars();
		for (var i = 0; i < params.length; i++) {
			var key = params[i].toLowerCase();
			if (!isQueryParameterImportant(vars, key)) {
				continue;
			}
			var value = vars[key];
			if (!value) {
				continue;
			}
			if (text.length > 0) {
				text = text + ", ";
			}
			var type = vars[key + "type"];
			var operator = typeToOperator(type);
			text = text + params[i] + " " + operator + " " + value;
		}
		$(this).text(text);
	});
}

/**
 * hides redundant columns to preserve space
 */
function hideRedundantColumns() {
	var vars = getUrlVars();
	if (vars["branch"] && vars["branchtype"] === "match") {
		$("th[data-field='5'").remove();
//		$('#table').bootstrapTable("hideColumn", "5");
	}
	if (vars["repository"] && vars["repositorytype"] === "match") {
		$("th[data-field='0'").remove();
//		$('#table').bootstrapTable("hideColumn", "0");
	}
}


/**
 * loads the search result from the server
 */
function initTable() {
	$.getJSON( "api.py" + window.location.search, function( data ) {
		if (typeof data === "string") {
			alert(data);
			return;
		}
		window.config = data.config;
		window.repositories = data.repositories;
		hideRedundantColumns();
		$("#table").bootstrapTable();
		$("#table").bootstrapTable("load", {data: data.data});
		if (data.data.length > 0) {
			mergeCells(data.data);
		}
		$("#table").removeClass("hidden");
		$(".spinner").addClass("hidden");
	});
}

// http://stackoverflow.com/a/12034334
var entityMap = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	"\"": "&quot;",
	"'": "&#39;"
};
function escapeHtml(string) {
	return String(string).replace(/[&<>"']/g, function (s) {
		return entityMap[s];
	});
}


function guessSCM(revision) {
	if (revision.indexOf(".") >= 0) {
		return "cvs";
	} else if (revision.length < 40) {
		return "subversion";
	}
	return "git";
}

function calculatePreviousCvsRevision(revision) {
	var split = revision.split(".");
	var last = split[split.length - 1];
	if (last === "1" && split.length > 2) {
		split.pop();
		split.pop();
	} else {
		split[split.length - 1] = parseInt(last) - 1;
	}
	return split.join(".");	
}

function rowToProp(row) {
	var scm = guessSCM(row[4]);
	var prop = {
		"[repository]": escapeHtml(row[0].replace("/srv/cvs/", "").replace("/var/lib/cvs/")),
		"[file]" : escapeHtml(row[3]),
		"[revision]": escapeHtml(row[4]),
		"[short_revision]": escapeHtml(row[4]),
		"[scm]": scm
	};
	if (scm === "cvs") {
		prop["[old_revision]"] = escapeHtml(calculatePreviousCvsRevision(row[4]));
	}
	if (scm === "git") {
		prop["[short_revision]"] = escapeHtml(row[4].substring(0, 8));
	}
	return prop;
}

function argsubst(str, prop) {
	for (var key in prop) {
		if (prop.hasOwnProperty(key)) {
			var value = prop[key];
			while(str.indexOf(key) > -1) {
				str = str.replace(key, value);
			}
		}
	}
	return str;
}


function formatTimestamp(value, row, index) {
	if (!value) {
		return "-";
	}
	return escapeHtml(value.substring(0, 16));
}

function readRepositoryConfig(repo, key, fallback) {
	var repoConfig = window.repositories ? window.repositories[repo] : null;
	return repoConfig ? repoConfig[key] : fallback;	
}

/**
 * formats the description column to link to an issue tracker
 */
function formatTrackerLink(value, row, index) {
	if (!value) {
		return "-";
	}
	var res = escapeHtml(value);
	var url = readRepositoryConfig(row[0], "tracker_url", window.config.tracker);
	if (!url) {
		return res;
	}

	return res.replace(/#([0-9]*)/g, "<a href='" + url + "'>#$1</a>");
}


/**
 * formats the rev column to link to viewvc file content
 */
function formatFileLink(value, row, index) {
	if (!value) {
		return "-";
	}
	var prop = rowToProp(row);
	var url = readRepositoryConfig(row[0], "file_url", null);
	if (!url) {
		return escapeHtml(value);
	}
	return argsubst("<a href='" + url + "'>[file]</a>", prop);
}

/**
 * format the diff column to link to the difference
 */
function formatDiffLink(value, row, index) {
	if (!value) {
		return "-";
	}
	var prop = rowToProp(row);
	var url = readRepositoryConfig(row[0], "commit_url", null);
	if (!url) {
		return prop["[short_revision]"];
	}
	return argsubst("<a href='" + url + "'>[short_revision]</a>", prop);
}


// export functions
window["formatFileLink"] = formatFileLink;
window["formatTimestamp"] = formatTimestamp;
window["formatTrackerLink"] = formatTrackerLink;
window["formatDiffLink"] = formatDiffLink;

$("ready", function() {
	window.config = {};
	addQueryStringToLink();
	addValuesFromURLs();
	if (document.querySelector("body.page-searchresult")) {
		renderQueryParameters();
		initTable();
	}
});
}());
