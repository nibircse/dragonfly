﻿#
# This file is part of Dragonfly.
# (c) Copyright 2007, 2008 by Christo Butcher
# Licensed under the LGPL.
#
#   Dragonfly is free software: you can redistribute it and/or modify it 
#   under the terms of the GNU Lesser General Public License as published 
#   by the Free Software Foundation, either version 3 of the License, or 
#   (at your option) any later version.
#
#   Dragonfly is distributed in the hope that it will be useful, but 
#   WITHOUT ANY WARRANTY; without even the implied warranty of 
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU 
#   Lesser General Public License for more details.
#
#   You should have received a copy of the GNU Lesser General Public 
#   License along with Dragonfly.  If not, see 
#   <http://www.gnu.org/licenses/>.
#

"""
This file implements the SAPI 5 engine back-end.
"""


#---------------------------------------------------------------------------

import win32com.client
from win32com.client import constants
from pywintypes import com_error

import dragonfly.grammar.state as state_
from dragonfly.engines.engine_base import EngineBase
from dragonfly.engines.compiler_sapi5 import Sapi5Compiler
from dragonfly.windows.window import Window


#---------------------------------------------------------------------------

class Sapi5Engine(EngineBase):

    """
        Speech recognition engine back-end for SAPI 5.
    """

    @classmethod
    def is_available(cls):
        """ Check whether this engine is available. """
        try:
            win32com.client.Dispatch("SAPI.SpSharedRecognizer")
        except com_error:
            return False
        return True


    #-----------------------------------------------------------------------

    def __init__(self):
        self._recognizer = win32com.client.Dispatch("SAPI.SpSharedRecognizer")
        self._speaker = win32com.client.Dispatch("SAPI.SpVoice")
        self._compiler = Sapi5Compiler()


    #-----------------------------------------------------------------------
    # Methods for working with grammars.

    def load_grammar(self, grammar):
        """ Load the given *grammar*. """
        self._log.debug("Loading grammar %s." % grammar.name)
        grammar.engine = self
        context = self._recognizer.CreateRecoContext()
        handle = self._compiler.compile_grammar(grammar, context)
        wrapper = GrammarWrapper(grammar, handle, context, self)
        self._set_grammar_wrapper(grammar, wrapper)

        self.activate_grammar(grammar)
        for l in grammar.lists:
            l._update()
        for r in grammar.rules:
            self.activate_rule(r, grammar)

    def activate_grammar(self, grammar):
        """ Activate the given *grammar*. """
        self._log.debug("Activating grammar %s." % grammar.name)
        grammar_handle = self._get_grammar_wrapper(grammar).handle
        grammar_handle.State = constants.SGSEnabled

        # Turn on dictation during loading of the grammar.
        grammar_handle.DictationSetState(constants.SGDSActive)

        for rule_handle in collection_iter(grammar_handle.Rules):
            grammar_handle.CmdSetRuleState(rule_handle.Name, constants.SGDSActive)

        # Turn off dictation after loading the grammar.
        grammar_handle.DictationSetState(constants.SGDSInactive)

    def deactivate_grammar(self, grammar):
        """ Deactivate the given *grammar*. """
        self._log.debug("Deactivating grammar %s." % grammar.name)
        grammar_handle = self._get_grammar_wrapper(grammar).handle
        grammar_handle.State = constants.SGSDisabled

    def activate_rule(self, rule, grammar):
        """ Activate the given *rule*. """
        self._log.debug("Activating rule %s in grammar %s."
                        % (rule.name, grammar.name))
        grammar_handle = self._get_grammar_wrapper(grammar).handle
        grammar_handle.Rules.Commit()
        attributes = grammar_handle.Rules.FindRule(rule.name).Attributes
        grammar_handle.CmdSetRuleState(rule.name, constants.SGDSActive)
        grammar_handle.Rules.CommitAndSave()

    def deactivate_rule(self, rule, grammar):
        """ Deactivate the given *rule*. """
        self._log.debug("Deactivating rule %s in grammar %s."
                        % (rule.name, grammar.name))
        grammar_handle = self._get_grammar_wrapper(grammar).handle
        grammar_handle.Rules.Commit()
        attributes = grammar_handle.Rules.FindRule(rule.name).Attributes
        grammar_handle.CmdSetRuleState(rule.name, constants.SGDSInactive)
        grammar_handle.Rules.CommitAndSave()

    def update_list(self, lst, grammar):
        grammar_handle = self._get_grammar_wrapper(grammar).handle
        list_rule_name = "__list_%s" % lst.name
        rule_handle = grammar_handle.Rules.FindRule(list_rule_name)

        rule_handle.Clear()
        src_state = rule_handle.InitialState
        dst_state = None
        for item in lst.get_list_items():
            src_state.AddWordTransition(dst_state, item)

        grammar_handle.Rules.Commit()

    def _set_grammar_wrapper(self, grammar, grammar_wrapper):
        grammar._grammar_wrapper = grammar_wrapper

    def _get_grammar_wrapper(self, grammar):
        return grammar._grammar_wrapper


    #-----------------------------------------------------------------------
    # Methods for handling dictation elements.

    def format_dictation_node(self, node):
        results = node.full_results()
        self._log.debug("%s: format the dictation of the %r."
                        % (self, results))
        return " ".join(r[2] for r in results)


    #-----------------------------------------------------------------------
    # Miscellaneous methods.

    def mimic(self, words):
        """ Mimic a recognition of the given *words*. """
        if isinstance(words, basestring):
            phrase = words
        else:
            phrase = " ".join(words)
        result = self._recognizer.EmulateRecognition(phrase)
        self._log.error("Emulate results: %r" % result )

    def speak(self, text):
        """ Speak the given *text* using text-to-speech. """
        self._speaker.Speak(text)


#---------------------------------------------------------------------------
# Utility generator function for iterating over COM collections.

def collection_iter(collection):
    if not collection:
        return
    for index in xrange(0, collection.Count):
        yield collection.Item(index)


#---------------------------------------------------------------------------

class GrammarWrapper(object):

    def __init__(self, grammar, handle, context, engine):
        self.grammar = grammar
        self.handle = handle
        self.engine = engine

        base = win32com.client.getevents("SAPI.SpSharedRecoContext")
        class ContextEvents(base): pass
        c = ContextEvents(context)
        c.OnRecognition = self.recognition_callback
        c.OnPhraseStart = self.phrase_start_callback

    def phrase_start_callback(self, stream_number, stream_position):
        window = Window.get_foreground()
        self.grammar.process_begin(window.executable, window.title,
                                   window.handle)

    def recognition_callback(self, StreamNumber, StreamPosition, RecognitionType, Result):
        try:
            newResult = win32com.client.Dispatch(Result)
            phrase_info = newResult.PhraseInfo
            rule_name = phrase_info.Rule.Name

            #---------------------------------------------------------------
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Speak('you said '+phrase_info.GetText())

            #---------------------------------------------------------------
            # Build a list of rule names for each element.

            # First populate it with the top level rule name.
            element = phrase_info.Rule
            name = element.Name
            start = element.FirstElement
            count = element.NumberOfElements
            rule_names = [name] * count

            # Walk the tree of child rules and put their names in the list.
            stack = [collection_iter(phrase_info.Rule.Children)]
            while stack:
                try: element = stack[-1].next()
                except StopIteration: stack.pop(); continue
                name = element.Name
                start = element.FirstElement
                count = element.NumberOfElements
                rule_names[start:start + count] = [name] * count
                if element.Children:
                    stack.append(collection_iter(element.Children))

            #---------------------------------------------------------------
            # Prepare the words and rule names for the element parsers.

            replacements = [False] * len(rule_names)
            if phrase_info.Replacements:
                for replacement in collection_iter(phrase_info.Replacements):
                    begin = replacement.FirstElement
                    end = begin + replacement.NumberOfElements
                    replacements[begin] = replacement.Text
                    for index in range(begin + 1, end):
                        replacements[index] = True

            results = []
            rule_set = list(set(rule_names))
            elements = phrase_info.Elements
            for index in range(len(rule_names)):
                element = elements.Item(index)
                rule_id = rule_set.index(rule_names[index])
                replacement = replacements[index]
                info = [element.LexicalForm, rule_id,
                        element.DisplayText, element.DisplayAttributes,
                        replacement]
                results.append(info)

            #---------------------------------------------------------------
            # Attempt to parse the recognition.

            s = state_.State(results, rule_set, self.engine)
            for r in self.grammar._rules:
                if r.name != rule_name:
                    continue
                s.initialize_decoding()
                for result in r.decode(s):
                    if s.finished():
                        root = s.build_parse_tree()
                        r.process_recognition(root)
                        return

        except Exception, e:
            Sapi5Engine._log.error("Grammar %s: exception: %s"
                                   % (self.grammar._name, e), exc_info=True)

        #-------------------------------------------------------------------
        # If this point is reached, then the recognition was not
        #  processed successfully..

        self.engine._log.error("Grammar %s: failed to decode"
                               " recognition %r."
                               % (self.grammar._name,
                                  [r[0] for r in results]))
